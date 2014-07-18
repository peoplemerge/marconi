# Copyright (c) 2013 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import datetime
import time
import uuid

import mock
from pymongo import cursor
import pymongo.errors
import six
from testtools import matchers

from marconi.openstack.common.cache import cache as oslo_cache
from marconi.openstack.common import timeutils
from marconi.queues import storage
from marconi.queues.storage import errors
from marconi.queues.storage import mongodb
from marconi.queues.storage.mongodb import controllers
from marconi.queues.storage.mongodb import options
from marconi.queues.storage.mongodb import utils
from marconi.queues.storage import pooling
from marconi import tests as testing
from marconi.tests.queues.storage import base


class MongodbDBSetup(testing.TestBase):
    def _purge_databases(self):
        databases = (self.driver.message_databases +
                     [self.driver.queues_database])

        for db in databases:
            self.driver.connection.drop_database(db)

    def _prepare_conf(self):
        self.config(options.MONGODB_GROUP,
                    database=uuid.uuid4().hex)


class MongodbUtilsTest(testing.TestBase):

    config_file = 'wsgi_mongodb.conf'

    def setUp(self):
        super(MongodbUtilsTest, self).setUp()

        self.conf.register_opts(options.MONGODB_OPTIONS,
                                group=options.MONGODB_GROUP)

        self.mongodb_conf = self.conf[options.MONGODB_GROUP]

        MockDriver = collections.namedtuple('MockDriver', 'mongodb_conf')

        self.driver = MockDriver(self.mongodb_conf)

    def test_scope_queue_name(self):
        self.assertEqual(utils.scope_queue_name('my-q'), '/my-q')
        self.assertEqual(utils.scope_queue_name('my-q', None), '/my-q')
        self.assertEqual(utils.scope_queue_name('my-q', '123'), '123/my-q')

        self.assertEqual(utils.scope_queue_name(None), '/')
        self.assertEqual(utils.scope_queue_name(None, '123'), '123/')

    def test_descope_queue_name(self):
        self.assertEqual(utils.descope_queue_name('/'), None)
        self.assertEqual(utils.descope_queue_name('/some-pig'), 'some-pig')
        self.assertEqual(utils.descope_queue_name('radiant/some-pig'),
                         'some-pig')

    def test_calculate_backoff(self):
        sec = utils.calculate_backoff(0, 10, 2, 0)
        self.assertEqual(sec, 0)

        sec = utils.calculate_backoff(9, 10, 2, 0)
        self.assertEqual(sec, 1.8)

        sec = utils.calculate_backoff(4, 10, 2, 0)
        self.assertEqual(sec, 0.8)

        sec = utils.calculate_backoff(4, 10, 2, 1)
        if sec != 0.8:
            self.assertThat(sec, matchers.GreaterThan(0.8))
            self.assertThat(sec, matchers.LessThan(1.8))

        self.assertRaises(ValueError, utils.calculate_backoff, 0, 10, -2, -1)
        self.assertRaises(ValueError, utils.calculate_backoff, 0, 10, -2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, 0, 10, 2, -1)

        self.assertRaises(ValueError, utils.calculate_backoff, -2, -10, 2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, 2, -10, 2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, -2, 10, 2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, -1, 10, 2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, 10, 10, 2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, 11, 10, 2, 0)

    def test_retries_on_autoreconnect(self):
        num_calls = [0]

        @utils.retries_on_autoreconnect
        def _raises_autoreconnect(self):
            num_calls[0] += 1
            raise pymongo.errors.AutoReconnect()

        self.assertRaises(pymongo.errors.AutoReconnect,
                          _raises_autoreconnect, self)
        self.assertEqual(num_calls, [self.mongodb_conf.max_reconnect_attempts])

    def test_retries_on_autoreconnect_neg(self):
        num_calls = [0]

        @utils.retries_on_autoreconnect
        def _raises_autoreconnect(self):
            num_calls[0] += 1

            # NOTE(kgriffs): Don't exceed until the last attempt
            if num_calls[0] < self.mongodb_conf.max_reconnect_attempts:
                raise pymongo.errors.AutoReconnect()

        # NOTE(kgriffs): Test that this does *not* raise AutoReconnect
        _raises_autoreconnect(self)

        self.assertEqual(num_calls, [self.mongodb_conf.max_reconnect_attempts])


@testing.requires_mongodb
class MongodbDriverTest(testing.TestBase):

    config_file = 'wsgi_mongodb.conf'

    def test_db_instance(self):
        cache = oslo_cache.get_cache()
        driver = mongodb.DataDriver(self.conf, cache)

        databases = (driver.message_databases +
                     [driver.queues_database])

        for db in databases:
            self.assertThat(db.name, matchers.StartsWith(
                driver.mongodb_conf.database))


@testing.requires_mongodb
class MongodbQueueTests(base.QueueControllerTest):

    driver_class = mongodb.DataDriver
    config_file = 'wsgi_mongodb.conf'
    controller_class = controllers.QueueController

    def test_indexes(self):
        collection = self.controller._collection
        indexes = collection.index_information()
        self.assertIn('p_q_1', indexes)

    def test_messages_purged(self):
        queue_name = 'test'
        self.controller.create(queue_name)
        self.message_controller.post(queue_name,
                                     [{'ttl': 60}],
                                     1234)
        self.controller.delete(queue_name)
        for collection in self.message_controller._collections:
            self.assertEqual(collection.find({'q': queue_name}).count(), 0)

    def test_raises_connection_error(self):

        with mock.patch.object(cursor.Cursor,
                               'next' if six.PY2 else '__next__',
                               autospec=True) as method:
            error = pymongo.errors.ConnectionFailure()
            method.side_effect = error

            queues = next(self.controller.list())
            self.assertRaises(storage.errors.ConnectionError,
                              queues.next)


@testing.requires_mongodb
class MongodbMessageTests(base.MessageControllerTest):

    driver_class = mongodb.DataDriver
    config_file = 'wsgi_mongodb.conf'
    controller_class = controllers.MessageController

    # NOTE(kgriffs): MongoDB's TTL scavenger only runs once a minute
    gc_interval = 60

    def test_indexes(self):
        for collection in self.controller._collections:
            indexes = collection.index_information()
            self.assertIn('active', indexes)
            self.assertIn('claimed', indexes)
            self.assertIn('queue_marker', indexes)
            self.assertIn('counting', indexes)

    def test_message_counter(self):
        queue_name = self.queue_name
        iterations = 10

        seed_marker1 = self.queue_controller._get_counter(queue_name,
                                                          self.project)
        self.assertEqual(seed_marker1, 1, 'First marker is 1')

        for i in range(iterations):
            self.controller.post(queue_name, [{'ttl': 60}],
                                 'uuid', project=self.project)

            marker1 = self.queue_controller._get_counter(queue_name,
                                                         self.project)
            marker2 = self.queue_controller._get_counter(queue_name,
                                                         self.project)
            marker3 = self.queue_controller._get_counter(queue_name,
                                                         self.project)

            self.assertEqual(marker1, marker2)
            self.assertEqual(marker2, marker3)
            self.assertEqual(marker1, i + 2)

        new_value = self.queue_controller._inc_counter(queue_name,
                                                       self.project)
        self.assertIsNotNone(new_value)

        value_before = self.queue_controller._get_counter(queue_name,
                                                          project=self.project)
        new_value = self.queue_controller._inc_counter(queue_name,
                                                       project=self.project)
        self.assertIsNotNone(new_value)
        value_after = self.queue_controller._get_counter(queue_name,
                                                         project=self.project)
        self.assertEqual(value_after, value_before + 1)

        value_before = value_after
        new_value = self.queue_controller._inc_counter(queue_name,
                                                       project=self.project,
                                                       amount=7)
        value_after = self.queue_controller._get_counter(queue_name,
                                                         project=self.project)
        self.assertEqual(value_after, value_before + 7)
        self.assertEqual(value_after, new_value)

        reference_value = value_after

        unchanged = self.queue_controller._inc_counter(queue_name,
                                                       project=self.project,
                                                       window=10)
        self.assertIsNone(unchanged)

        now = timeutils.utcnow() + datetime.timedelta(seconds=10)
        timeutils_utcnow = 'marconi.openstack.common.timeutils.utcnow'
        with mock.patch(timeutils_utcnow) as mock_utcnow:
            mock_utcnow.return_value = now
            changed = self.queue_controller._inc_counter(queue_name,
                                                         project=self.project,
                                                         window=5)
            self.assertEqual(changed, reference_value + 1)

    def test_race_condition_on_post(self):
        queue_name = self.queue_name

        expected_messages = [
            {
                'ttl': 60,
                'body': {
                    'event': 'BackupStarted',
                    'backupId': 'c378813c-3f0b-11e2-ad92-7823d2b0f3ce',
                },
            },
            {
                'ttl': 60,
                'body': {
                    'event': 'BackupStarted',
                    'backupId': 'd378813c-3f0b-11e2-ad92-7823d2b0f3ce',
                },
            },
            {
                'ttl': 60,
                'body': {
                    'event': 'BackupStarted',
                    'backupId': 'e378813c-3f0b-11e2-ad92-7823d2b0f3ce',
                },
            },
        ]

        uuid = '97b64000-2526-11e3-b088-d85c1300734c'

        # NOTE(kgriffs): Patch _inc_counter so it is a noop, so that
        # the second time we post, we will get a collision. This simulates
        # what happens when we have parallel requests and the "winning"
        # requests hasn't gotten around to calling _inc_counter before the
        # "losing" request attempts to insert it's batch of messages.
        with mock.patch.object(mongodb.queues.QueueController,
                               '_inc_counter', autospec=True) as method:

            method.return_value = 2
            messages = expected_messages[:1]
            created = list(self.controller.post(queue_name, messages,
                                                uuid, project=self.project))
            self.assertEqual(len(created), 1)

            # Force infinite retries
            if testing.RUN_SLOW_TESTS:
                method.return_value = None

                with testing.expect(errors.MessageConflict):
                    self.controller.post(queue_name, messages,
                                         uuid, project=self.project)

        created = list(self.controller.post(queue_name,
                                            expected_messages[1:],
                                            uuid, project=self.project))

        self.assertEqual(len(created), 2)

        expected_ids = [m['body']['backupId'] for m in expected_messages]

        interaction = self.controller.list(queue_name, client_uuid=uuid,
                                           echo=True, project=self.project)

        actual_messages = list(next(interaction))
        self.assertEqual(len(actual_messages), len(expected_messages))
        actual_ids = [m['body']['backupId'] for m in actual_messages]

        self.assertEqual(actual_ids, expected_ids)

    def test_pop_messages(self):
        queue_name = 'pop-queue-test'
        self.queue_controller.create(queue_name, self.project)
        messages = [
            {
                'ttl': 60,
                'body': {
                    'event': 'BackupStarted',
                    'backupId': 'c378813c-3f0b-11e2-ad92-7823d2b0f3ce',
                },
            },
            {
                'ttl': 60,
                'body': {
                    'event': 'BackupStarted',
                    'backupId': 'd378813c-3f0b-11e2-ad92-7823d2b0f3ce',
                },
            },
            {
                'ttl': 60,
                'body': {
                    'event': 'BackupStarted',
                    'backupId': 'e378813c-3f0b-11e2-ad92-7823d2b0f3ce',
                },
            },
        ]
        self.controller.post(queue_name, messages, uuid.uuid1(), self.project)
        message_popped = self.controller.pop(queue_name,
                                             limit=1,
                                             project=self.project)
        self.assertEqual(len(message_popped), 1)

    def test_empty_queue_exception(self):
        self.assertRaises(storage.errors.QueueIsEmpty,
                          self.controller.first,
                          self.queue_name, project=self.project)

    def test_invalid_sort_option(self):
        self.assertRaises(ValueError,
                          self.controller.first,
                          self.queue_name, sort=0,
                          project=self.project)


@testing.requires_mongodb
class MongodbClaimTests(base.ClaimControllerTest):

    driver_class = mongodb.DataDriver
    config_file = 'wsgi_mongodb.conf'
    controller_class = controllers.ClaimController

    def test_claim_doesnt_exist(self):
        """Verifies that operations fail on expired/missing claims.

        Methods should raise an exception when the claim doesn't
        exists and/or has expired.
        """
        epoch = '000000000000000000000000'
        self.assertRaises(storage.errors.ClaimDoesNotExist,
                          self.controller.get, self.queue_name,
                          epoch, project=self.project)

        claim_id, messages = self.controller.create(self.queue_name,
                                                    {'ttl': 1, 'grace': 0},
                                                    project=self.project)

        # Lets let it expire
        time.sleep(1)
        self.assertRaises(storage.errors.ClaimDoesNotExist,
                          self.controller.update, self.queue_name,
                          claim_id, {}, project=self.project)

        self.assertRaises(storage.errors.ClaimDoesNotExist,
                          self.controller.update, self.queue_name,
                          claim_id, {}, project=self.project)


@testing.requires_mongodb
class MongodbPoolsTests(base.PoolsControllerTest):
    driver_class = mongodb.ControlDriver
    controller_class = controllers.PoolsController

    def setUp(self):
        super(MongodbPoolsTests, self).setUp()
        self.load_conf('wsgi_mongodb.conf')

    def tearDown(self):
        super(MongodbPoolsTests, self).tearDown()


@testing.requires_mongodb
class MongodbCatalogueTests(base.CatalogueControllerTest):
    driver_class = mongodb.ControlDriver
    controller_class = controllers.CatalogueController

    def setUp(self):
        super(MongodbCatalogueTests, self).setUp()
        self.load_conf('wsgi_mongodb.conf')

    def tearDown(self):
        self.controller.drop_all()
        super(MongodbCatalogueTests, self).tearDown()


@testing.requires_mongodb
class PooledMessageTests(base.MessageControllerTest):
    config_file = 'wsgi_mongodb_pooled.conf'
    controller_class = pooling.MessageController
    driver_class = pooling.DataDriver
    control_driver_class = mongodb.ControlDriver
    controller_base_class = pooling.RoutingController


@testing.requires_mongodb
class PooledQueueTests(base.QueueControllerTest):
    config_file = 'wsgi_mongodb_pooled.conf'
    controller_class = pooling.QueueController
    driver_class = pooling.DataDriver
    control_driver_class = mongodb.ControlDriver
    controller_base_class = pooling.RoutingController


@testing.requires_mongodb
class PooledClaimsTests(base.ClaimControllerTest):
    config_file = 'wsgi_mongodb_pooled.conf'
    controller_class = pooling.ClaimController
    driver_class = pooling.DataDriver
    control_driver_class = mongodb.ControlDriver
    controller_base_class = pooling.RoutingController
