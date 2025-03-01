# Copyright 1999-2020 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import defaultdict
from typing import Dict, List, Optional

from .... import oscar as mo
from ....utils import extensible
from ...meta.api import MetaAPI
from ...storage.api import StorageAPI
from ..errors import TileableNotTracked


class LifecycleTrackerActor(mo.Actor):
    _meta_api: MetaAPI

    def __init__(self,
                 session_id: str):
        self._session_id = session_id
        self._tileable_key_to_chunk_keys = dict()
        self._tileable_ref_counts = defaultdict(lambda: 0)
        self._chunk_ref_counts = defaultdict(lambda: 0)

        self._meta_api: Optional[MetaAPI] = None

    async def __post_create__(self):
        self._meta_api = await MetaAPI.create(self._session_id, self.address)

    async def __pre_destroy__(self):
        chunk_keys = [chunk_key for chunk_key, ref_count
                      in self._chunk_ref_counts.items() if ref_count > 0]
        # remove all chunks
        await self._remove_chunks(chunk_keys)

    @staticmethod
    def gen_uid(session_id):
        return f'{session_id}_lifecycle_tracker'

    @extensible
    def track(self, tileable_key: str, chunk_keys: List[str]):
        self._tileable_key_to_chunk_keys[tileable_key] = chunk_keys

    def incref_chunks(self, chunk_keys: List[str]):
        for chunk_key in chunk_keys:
            self._chunk_ref_counts[chunk_key] += 1

    def _get_remove_chunk_keys(self, chunk_keys: List[str]):
        to_remove_chunk_keys = []
        for chunk_key in chunk_keys:
            self._chunk_ref_counts[chunk_key] -= 1
            ref_count = self._chunk_ref_counts[chunk_key]
            assert ref_count >= 0
            if ref_count == 0:
                # remove
                to_remove_chunk_keys.append(chunk_key)
        return to_remove_chunk_keys

    async def decref_chunks(self, chunk_keys: List[str]):
        to_remove_chunk_keys = self._get_remove_chunk_keys(chunk_keys)
        return self._remove_chunks(to_remove_chunk_keys)

    async def _remove_chunks(self, to_remove_chunk_keys: List[str]):
        # get meta
        get_metas = []
        for to_remove_chunk_key in to_remove_chunk_keys:
            get_metas.append(
                self._meta_api.get_chunk_meta.delay(to_remove_chunk_key,
                                                    fields=['bands'],
                                                    error='ignore'))
        metas = await self._meta_api.get_chunk_meta.batch(*get_metas)

        # filter chunks that not exist
        new_to_remove_chunk_keys = []
        new_metas = []
        for to_remove_chunk_key, meta in zip(to_remove_chunk_keys, metas):
            if meta is not None:
                new_to_remove_chunk_keys.append(to_remove_chunk_key)
                new_metas.append(meta)
        to_remove_chunk_keys = new_to_remove_chunk_keys
        metas = new_metas

        all_bands = [meta['bands'] for meta in metas]
        key_to_addresses = dict()
        for to_remove_chunk_key, bands in zip(to_remove_chunk_keys, all_bands):
            key_to_addresses[to_remove_chunk_key] = [band[0] for band in bands]

        # remove data via storage API
        storage_api_to_deletes = defaultdict(list)
        for key, addresses in key_to_addresses.items():
            for addr in addresses:
                # storage API is cached for same arguments
                storage_api = await StorageAPI.create(self._session_id, addr)
                storage_api_to_deletes[storage_api].append(
                    storage_api.delete.delay(key, error='ignore'))
        for storage_api, deletes in storage_api_to_deletes.items():
            await storage_api.delete.batch(*deletes)

        # delete meta
        delete_metas = []
        for to_remove_chunk_key in to_remove_chunk_keys:
            delete_metas.append(
                self._meta_api.del_chunk_meta.delay(to_remove_chunk_key))
        await self._meta_api.del_chunk_meta.batch(*delete_metas)

    def get_chunk_ref_counts(self, chunk_keys: List[str]) -> List[int]:
        return [self._chunk_ref_counts[chunk_key]
                for chunk_key in chunk_keys]

    def get_all_chunk_ref_counts(self) -> Dict[str, int]:
        result = dict()
        for chunk_key, ref_count in self._chunk_ref_counts.items():
            if ref_count > 0:
                result[chunk_key] = ref_count
        return result

    def incref_tileables(self, tileable_keys: List[str]):
        for tileable_key in tileable_keys:
            if tileable_key not in self._tileable_key_to_chunk_keys:
                raise TileableNotTracked(f'tileable {tileable_key} '
                                         f'not tracked before')
            self._tileable_ref_counts[tileable_key] += 1
            # incref chunks for this tileable
            self.incref_chunks(
                self._tileable_key_to_chunk_keys[tileable_key])

    async def decref_tileables(self, tileable_keys: List[str]):
        to_remove_chunk_keys = []
        for tileable_key in tileable_keys:
            if tileable_key not in self._tileable_key_to_chunk_keys:
                raise TileableNotTracked(f'tileable {tileable_key} '
                                         f'not tracked before')
            self._tileable_ref_counts[tileable_key] -= 1

            to_remove_chunk_keys.extend(self._get_remove_chunk_keys(
                self._tileable_key_to_chunk_keys[tileable_key]))

        return self._remove_chunks(to_remove_chunk_keys)

    def get_tileable_ref_counts(self, tileable_keys: List[str]) -> List[int]:
        return [self._tileable_ref_counts[tileable_key]
                for tileable_key in tileable_keys]
