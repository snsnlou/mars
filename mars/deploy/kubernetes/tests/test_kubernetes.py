# -*- coding: utf-8 -*-
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

import glob
import logging
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from distutils.spawn import find_executable

import numpy as np
from numpy.testing import assert_array_equal

import mars.tensor as mt
from mars.deploy.kubernetes import new_cluster
from mars.deploy.kubernetes.config import HostPathVolumeConfig
from mars.tests.core import mock

try:
    from kubernetes import config as k8s_config, client as k8s_client
except ImportError:
    k8s_client = k8s_config = None

MARS_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(mt.__file__)))
TEST_ROOT = os.path.dirname(os.path.abspath(__file__))


@unittest.skipIf(
    find_executable('kubectl') is None or find_executable('docker') is None
    or k8s_config is None,
    reason='Cannot run without kubernetes')
class Test(unittest.TestCase):
    def setUp(self):
        logging.basicConfig(level=logging.INFO)

    def tearDown(self):
        logging.basicConfig(level=logging.WARNING)

        dist_coverage_path = os.path.join(MARS_ROOT, '.dist-coverage')
        if os.path.exists(dist_coverage_path):
            # change ownership of coverage files
            if find_executable('sudo'):
                proc = subprocess.Popen(['sudo', '-n', 'chown', '-R', f'{os.geteuid()}:{os.getegid()}',
                                         dist_coverage_path], shell=False)
                proc.wait()

            # rewrite paths in coverage result files
            for fn in glob.glob(os.path.join(dist_coverage_path, '.coverage.*')):
                if 'COVERAGE_FILE' in os.environ:
                    new_cov_file = os.environ['COVERAGE_FILE'] \
                                   + os.path.basename(fn).replace('.coverage', '')
                else:
                    new_cov_file = fn.replace('.dist-coverage' + os.sep, '')
                shutil.copyfile(fn, new_cov_file)
            shutil.rmtree(dist_coverage_path)

    @classmethod
    def _build_docker_images(cls):
        try:
            cls._docker_image = 'mars-test-image:' + uuid.uuid4().hex
            proc = subprocess.Popen(['docker', 'build',
                                     '-f', 'Dockerfile.test',
                                     '-t', cls._docker_image,
                                     '.'], cwd=TEST_ROOT)
            if proc.wait() != 0:
                raise SystemError('Executing docker build failed.')
            proc = subprocess.Popen(['docker', 'run',
                                     '-v', MARS_ROOT + ':/mnt/mars',
                                     cls._docker_image, '/srv/build_ext.sh'])
            if proc.wait() != 0:
                raise SystemError('Executing docker run failed.')
        except:  # noqa: E722
            cls._remove_docker_image()
            raise

    @classmethod
    def _remove_docker_image(cls, raises=True):
        proc = subprocess.Popen(['docker', 'rmi', '-f', cls._docker_image])
        if proc.wait() != 0 and raises:
            raise SystemError('Executing docker rmi failed.')

    @contextmanager
    def _start_kube_cluster(self, **kwargs):
        self._build_docker_images()

        temp_spill_dir = tempfile.mkdtemp(prefix='test-mars-k8s-')
        api_client = k8s_config.new_client_from_config()
        kube_api = k8s_client.CoreV1Api(api_client)

        cluster_client = None
        try:
            extra_vol_config = HostPathVolumeConfig('mars-src-path', '/mnt/mars', MARS_ROOT)
            cluster_client = new_cluster(api_client, image=self._docker_image,
                                         worker_spill_paths=[temp_spill_dir],
                                         extra_volumes=[extra_vol_config],
                                         pre_stop_command=['rm', '/tmp/stopping.tmp'],
                                         timeout=600, log_when_fail=True, **kwargs)
            self.assertIsNotNone(cluster_client.endpoint)

            pod_items = kube_api.list_namespaced_pod(cluster_client.namespace).to_dict()

            log_processes = []
            for item in pod_items['items']:
                log_processes.append(subprocess.Popen(
                    ['kubectl', 'logs', '-f', '-n', cluster_client.namespace,
                    item['metadata']['name']]))

            yield cluster_client

            # turn off service processes with grace to get coverage data
            procs = []
            pod_items = kube_api.list_namespaced_pod(cluster_client.namespace).to_dict()
            for item in pod_items['items']:
                p = subprocess.Popen(['kubectl', 'exec', '-n', cluster_client.namespace,
                                      item['metadata']['name'], '--', '/srv/graceful_stop.sh'])
                procs.append(p)
            for p in procs:
                p.wait()

            [p.terminate() for p in log_processes]
        finally:
            shutil.rmtree(temp_spill_dir)
            if cluster_client:
                try:
                    cluster_client.stop(wait=True, timeout=20)
                except TimeoutError:
                    pass
            self._remove_docker_image(False)

    def testRunInKubernetes(self):
        with self._start_kube_cluster(
                extra_labels={'mars-test/group': 'test-label-name'},
                extra_env={'MARS_K8S_GROUP_LABELS': 'mars-test/group'}):
            a = mt.ones((100, 100), chunk_size=30) * 2 * 1 + 1
            b = mt.ones((100, 100), chunk_size=20) * 2 * 1 + 1
            c = (a * b * 2 + 1).sum()
            r = c.execute().fetch()

            expected = (np.ones(a.shape) * 2 * 1 + 1) ** 2 * 2 + 1
            assert_array_equal(r, expected.sum())

    @mock.patch('kubernetes.client.CoreV1Api.create_namespaced_replication_controller',
                new=lambda *_, **__: None)
    def testCreateTimeout(self):
        api_client = k8s_config.new_client_from_config()

        cluster = None
        self._docker_image = 'pseudo_image'
        try:
            extra_vol_config = HostPathVolumeConfig('mars-src-path', '/mnt/mars', MARS_ROOT)
            with self.assertRaises(TimeoutError):
                cluster = new_cluster(api_client, image=self._docker_image,
                                      extra_volumes=[extra_vol_config], timeout=1)
        finally:
            if cluster:
                cluster.stop(wait=True)
