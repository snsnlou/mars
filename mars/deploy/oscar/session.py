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

import asyncio
from dataclasses import dataclass
from numbers import Integral
from urllib.parse import urlparse
from weakref import WeakKeyDictionary
from typing import Dict

from ...core import Tileable, enter_mode
from ...core.session import AbstractSession, register_session_cls, \
    ExecutionInfo as AbstractExecutionInfo, gen_submit_tileable_graph
from ...services.lifecycle import AbstractLifecycleAPI, LifecycleAPI
from ...services.meta import MetaAPI, AbstractMetaAPI
from ...services.session import AbstractSessionAPI, SessionAPI
from ...services.storage import StorageAPI
from ...services.task import AbstractTaskAPI, TaskAPI, TaskResult
from ...utils import implements, merge_chunks, register_asyncio_task_timeout_detector
from .typing import ClientType


@dataclass
class Progress:
    value: float = 0.0


class ExecutionInfo(AbstractExecutionInfo):
    def __init__(self,
                 task_id: str,
                 task_api: AbstractTaskAPI,
                 aio_task: asyncio.Task,
                 progress: Progress):
        super().__init__(aio_task)
        self._task_api = task_api
        self._task_id = task_id
        self._progress = progress

    def progress(self) -> float:
        return self._progress.value


@register_session_cls
class Session(AbstractSession):
    name = 'oscar'

    def __init__(self,
                 address: str,
                 session_id: str,
                 session_api: AbstractSessionAPI,
                 meta_api: AbstractMetaAPI,
                 lifecycle_api: AbstractLifecycleAPI,
                 task_api: AbstractTaskAPI,
                 client: ClientType = None):
        super().__init__(address, session_id)
        self._session_api = session_api
        self._task_api = task_api
        self._meta_api = meta_api
        self._lifecycle_api = lifecycle_api
        self.client = client

        self._tileable_to_fetch = WeakKeyDictionary()
        self._asyncio_task_timeout_detector_task = register_asyncio_task_timeout_detector()

    @classmethod
    async def _init(cls,
                    address: str,
                    session_id: str):
        session_api = await SessionAPI.create(address)
        # create new session
        session_address = await session_api.create_session(session_id)
        lifecycle_api = await LifecycleAPI.create(session_id, session_address)
        meta_api = await MetaAPI.create(session_id, session_address)
        task_api = await TaskAPI.create(session_id, session_address)
        return cls(address, session_id,
                   session_api, meta_api,
                   lifecycle_api, task_api)

    @classmethod
    @implements(AbstractSession.init)
    async def init(cls,
                   address: str,
                   session_id: str,
                   **kwargs) -> "Session":
        init_local = kwargs.pop('init_local', False)
        if init_local:
            from .local import new_cluster
            return (await new_cluster(address, **kwargs)).session

        if kwargs:  # pragma: no cover
            unexpected_keys = ', '.join(list(kwargs.keys()))
            raise TypeError(f'Oscar session got unexpected '
                            f'arguments: {unexpected_keys}')

        if urlparse(address).scheme == 'http':
            return await WebSession._init(address, session_id)
        else:
            return await Session._init(address, session_id)

    async def _run_in_background(self,
                                 tileables: list,
                                 task_id: str,
                                 progress: Progress):
        # wait for task to finish
        while True:
            task_result: TaskResult = await self._task_api.wait_task(
                task_id, timeout=0.5)
            if task_result is None:
                # not finished, set progress
                progress.value = await self._task_api.get_task_progress(task_id)
            else:
                progress.value = 1.0
                break
        if task_result.error:
            raise task_result.error.with_traceback(task_result.traceback)
        fetch_tileables = await self._task_api.get_fetch_tileables(task_id)
        assert len(tileables) == len(fetch_tileables)
        for tileable, fetch_tileable in zip(tileables, fetch_tileables):
            self._tileable_to_fetch[tileable] = fetch_tileable
            # update meta, e.g. unknown shape
            tileable.params = fetch_tileable.params

    async def execute(self,
                      *tileables,
                      **kwargs) -> ExecutionInfo:
        fuse_enabled: bool = kwargs.pop('fuse_enabled', False)
        task_name: str = kwargs.pop('task_name', None)
        extra_config: dict = kwargs.pop('extra_config', None)
        if kwargs:  # pragma: no cover
            raise TypeError(f'run got unexpected key arguments {list(kwargs)!r}')

        tileables = [tileable.data if hasattr(tileable, 'data') else tileable
                     for tileable in tileables]

        # build tileable graph
        tileable_graph = gen_submit_tileable_graph(self, tileables)

        # submit task
        task_id = await self._task_api.submit_tileable_graph(
            tileable_graph, task_name=task_name, fuse_enabled=fuse_enabled,
            extra_config=extra_config)

        progress = Progress()
        # create asyncio.Task
        future = asyncio.create_task(
            self._run_in_background(tileables, task_id, progress))
        return ExecutionInfo(task_id, self._task_api, future, progress)

    @enter_mode(build=True)
    def _get_to_fetch_tileable(self, tileable: Tileable):
        from ...tensor.indexing import TensorIndex
        from ...dataframe.indexing.iloc import \
            DataFrameIlocGetItem, SeriesIlocGetItem

        slice_op_types = \
            TensorIndex, DataFrameIlocGetItem, SeriesIlocGetItem

        if hasattr(tileable, 'data'):
            tileable = tileable.data

        indexes = None
        while tileable not in self._tileable_to_fetch:
            # if tileable's op is slice, try to check input
            if isinstance(tileable.op, slice_op_types):
                indexes = tileable.op.indexes
                tileable = tileable.inputs[0]
                if not all(isinstance(index, (slice, Integral))
                           for index in indexes):
                    raise ValueError('Only support fetch data slices')
            else:
                raise ValueError(f'Cannot fetch unexecuted '
                                 f'tileable: {tileable}')

        return self._tileable_to_fetch[tileable], indexes

    async def fetch(self, *tileables, **kwargs):
        if kwargs:  # pragma: no cover
            unexpected_keys = ', '.join(list(kwargs.keys()))
            raise TypeError(f'`fetch` got unexpected '
                            f'arguments: {unexpected_keys}')

        data = []
        for tileable in tileables:
            fetch_tileable, indexes = self._get_to_fetch_tileable(tileable)
            # TODO: support fetch slices
            assert indexes is None
            index_to_data = []
            for chunk in fetch_tileable.chunks:
                # TODO: use batch API to fetch data
                band = (await self._meta_api.get_chunk_meta(
                    chunk.key, fields=['bands']))['bands'][0]
                if urlparse(self.address).scheme == 'http':
                    from mars.services.storage import WebStorageAPI
                    storage_api = WebStorageAPI(self._session_id, self.address)
                else:
                    storage_api = await StorageAPI.create(self._session_id, band[0])
                index_to_data.append(
                    (chunk.index, await storage_api.get(chunk.key)))

            data.append(merge_chunks(index_to_data))

        return data

    async def decref(self, *tileable_keys):
        return await self._lifecycle_api.decref_tileables(list(tileable_keys))

    async def _get_ref_counts(self) -> Dict[str, int]:
        return await self._lifecycle_api.get_all_chunk_ref_counts()

    async def destroy(self):
        await super().destroy()
        await self._session_api.delete_session(self._session_id)
        if self._asyncio_task_timeout_detector_task:  # pragma: no cover
            self._asyncio_task_timeout_detector_task.cancel()

    async def stop_server(self):
        if self.client:
            await self.client.stop()


class WebSession(Session):
    @classmethod
    async def _init(cls,
                    address: str,
                    session_id: str):
        from ...services.session import WebSessionAPI
        from ...services.lifecycle import WebLifecycleAPI
        from ...services.meta import WebMetaAPI
        from ...services.task import WebTaskAPI

        session_api = WebSessionAPI(address)
        # create new session
        await session_api.create_session(session_id)
        lifecycle_api = WebLifecycleAPI(session_id, address)
        meta_api = WebMetaAPI(session_id, address)
        task_api = WebTaskAPI(session_id, address)

        return cls(address, session_id,
                   session_api, meta_api,
                   lifecycle_api, task_api)
