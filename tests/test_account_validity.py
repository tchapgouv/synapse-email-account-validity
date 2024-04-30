# -*- coding: utf-8 -*-
# Copyright 2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import time

# From Python 3.8 onwards, aiounittest.AsyncTestCase can be replaced by
# unittest.IsolatedAsyncioTestCase, so we'll be able to get rid of this dependency when
# we stop supporting Python < 3.8 in Synapse.
import aiounittest

from synapse.module_api import LoggingTransaction
from synapse.module_api.errors import SynapseError

from email_account_validity._utils import LONG_TOKEN_REGEX, SHORT_TOKEN_REGEX, TokenFormat, parse_duration
from tests import create_account_validity_module


class AccountValidityHooksTestCase(aiounittest.AsyncTestCase):
    async def test_user_expired(self):
        user_id = "@izzy:test"
        module = await create_account_validity_module()

        now_ms = int(time.time() * 1000)
        one_hour_ahead = now_ms + 3600000
        one_hour_ago = now_ms - 3600000

        # Test that, if the user isn't known, the module says it can't determine whether
        # they've expired.
        expired = await module.is_user_expired(user_id=user_id)

        self.assertIsNone(expired)

        # Test that, if the user has an expiration timestamp that's ahead of now, the
        # module says it can determine that they haven't expired.
        await module.renew_account_for_user(
            user_id=user_id,
            expiration_ts=one_hour_ahead,
        )

        expired = await module.is_user_expired(user_id=user_id)
        self.assertFalse(expired)

        # Test that, if the user has an expiration timestamp that's passed, the module
        # says it can determine that they have expired.
        await module.renew_account_for_user(
            user_id=user_id,
            expiration_ts=one_hour_ago,
        )

        expired = await module.is_user_expired(user_id=user_id)
        self.assertTrue(expired)

    async def test_on_user_registration(self):
        user_id = "@izzy:test"
        module = await create_account_validity_module()

        # Test that the user doesn't have an expiration date in the database. This acts
        # as a safeguard against old databases, and also adds an entry to the cache for
        # get_expiration_ts_for_user so we're sure later in the test that we've correctly
        # invalidated it.
        expiration_ts = await module._store.get_expiration_ts_for_user(user_id)

        self.assertIsNone(expiration_ts)

        # Call the registration hook and test that the user now has an expiration
        # timestamp that's ahead of now.
        await module.on_user_registration(user_id)

        expiration_ts = await module._store.get_expiration_ts_for_user(user_id)
        now_ms = int(time.time() * 1000)

        self.assertIsInstance(expiration_ts, int)
        self.assertGreater(expiration_ts, now_ms)


class AccountValidityEmailTestCase(aiounittest.AsyncTestCase):
    async def test_send_email(self):
        user_id = "@izzy:test"
        module = await create_account_validity_module()

        # Set the side effect of get_threepids_for_user so that it returns a threepid on
        # the first call and an empty list on the second call.

        threepids = [{
            "medium": "email",
            "address": "izzy@test",
        }]

        async def get_threepids(user_id):
            return threepids

        module._api.get_threepids_for_user.side_effect = get_threepids

        # Test that trying to send an email to an unknown user doesn't result in an email
        # being sent.
        try:
            await module.send_renewal_email_to_user(user_id)
        except SynapseError:
            pass

        self.assertEqual(module._api.send_mail.call_count, 0)

        await module._store.set_expiration_date_for_user(user_id)

        def check_email_status_account_validity(txn: LoggingTransaction):
            txn.execute(
                """
                SELECT eav.user_id, eav.expiration_ts_ms
                FROM email_account_validity eav 
                JOIN email_status_account_validity esav 
                ON eav.user_id = esav.user_id AND esav.email_sent = false
                """,
                ()
            )
            return txn.fetchall()
        email_status_users = await module._store._api.run_db_interaction("", check_email_status_account_validity, )
        self.assertEqual(3, len(email_status_users))

        # Test that trying to send an email to a known user that has an email address
        # attached to their account results in an email being sent
        await module.send_renewal_email_to_user(user_id)
        self.assertEqual(module._api.send_mail.call_count, 1)
        email_status_users = await module._store._api.run_db_interaction("", check_email_status_account_validity, )
        self.assertEqual(2, len(email_status_users))

        # Test that the email content contains a link; we haven't set send_links in the
        # module's config so its value should be the default (which is True).
        _, kwargs = module._api.send_mail.call_args
        path = "_synapse/client/email_account_validity/renew"
        self.assertNotEqual(kwargs["html"].find(path), -1)
        self.assertNotEqual(kwargs["text"].find(path), -1)

        # Test that trying to send an email to a known use that has no email address
        # attached to their account results in no email being sent.
        threepids = []
        await module.send_renewal_email_to_user(user_id)
        self.assertEqual(module._api.send_mail.call_count, 1)

    async def test_renewal_token(self):
        user_id = "@izzy:test"
        module = await create_account_validity_module()

        # Insert a row with an expiration timestamp and a renewal token for this user.
        await module._store.set_expiration_date_for_user(user_id)
        await module.generate_unauthenticated_renewal_token(user_id)

        # Retrieve the expiration timestamp and renewal token and check that they're in
        # the right format.
        old_expiration_ts = await module._store.get_expiration_ts_for_user(user_id)
        self.assertIsInstance(old_expiration_ts, int)

        renewal_token = await module._store.get_renewal_token_for_user(
            user_id,
            TokenFormat.LONG,
        )
        self.assertIsInstance(renewal_token, str)
        self.assertGreater(len(renewal_token), 0)
        self.assertTrue(LONG_TOKEN_REGEX.match(renewal_token))

        # Sleep a bit so the new expiration timestamp isn't likely to be equal to the
        # previous one.
        await asyncio.sleep(0.5)

        # Renew the account once with the token and test that the token is marked as
        # valid and the expiration timestamp has been updated.
        (
            token_valid,
            token_stale,
            new_expiration_ts,
        ) = await module.renew_account(renewal_token)

        self.assertTrue(token_valid)
        self.assertFalse(token_stale)
        self.assertGreater(new_expiration_ts, old_expiration_ts)

        # Renew the account a second time with the same token and test that, this time,
        # the token is marked as stale, and the expiration timestamp hasn't changed.
        (
            token_valid,
            token_stale,
            new_new_expiration_ts,
        ) = await module.renew_account(renewal_token)

        self.assertFalse(token_valid)
        self.assertTrue(token_stale)
        self.assertEqual(new_expiration_ts, new_new_expiration_ts)

        # Test that a fake token is marked as neither valid nor stale.
        (
            token_valid,
            token_stale,
            expiration_ts,
        ) = await module.renew_account("fake_token")

        self.assertFalse(token_valid)
        self.assertFalse(token_stale)
        self.assertEqual(expiration_ts, 0)

    async def test_duplicate_token(self):
        user_id_1 = "@izzy1:test"
        user_id_2 = "@izzy2:test"
        token = "sometoken"

        module = await create_account_validity_module()

        # Insert both users in the table.
        await module._store.set_expiration_date_for_user(user_id_1)
        await module._store.set_expiration_date_for_user(user_id_2)

        # Set the renewal token.
        await module._store.set_renewal_token_for_user(user_id_1, token, TokenFormat.LONG)

        # Try to set the same renewal token for another user.
        exception = None
        try:
            await module._store.set_renewal_token_for_user(
                user_id_2, token, TokenFormat.LONG,
            )
        except SynapseError as e:
            exception = e

        # Check that an exception was raised and that it's the one we're expecting.
        self.assertIsInstance(exception, SynapseError)
        self.assertEqual(exception.code, 500)

    async def test_send_link_false(self):
        user_id = "@izzy:test"
        # Create a module with a configuration forbidding it to send links via email.
        module = await create_account_validity_module({"send_links": False})

        async def get_threepids(user_id):
            return [{
                "medium": "email",
                "address": "izzy@test",
            }]

        module._api.get_threepids_for_user.side_effect = get_threepids
        await module._store.set_expiration_date_for_user(user_id)

        # Test that, when an email is sent, it doesn't include a link. We do this by
        # searching the email's content for the path for renewal requests.
        await module.send_renewal_email_to_user(user_id)
        self.assertEqual(module._api.send_mail.call_count, 1)

        _, kwargs = module._api.send_mail.call_args
        path = "_synapse/client/email_account_validity/renew"
        self.assertEqual(kwargs["html"].find(path), -1, kwargs["text"])
        self.assertEqual(kwargs["text"].find(path), -1, kwargs["text"])

        # Check that the renewal token is in the right format. It should be a 8 digit
        # long string.
        token = await module._store.get_renewal_token_for_user(user_id, TokenFormat.SHORT)
        self.assertIsInstance(token, str)
        self.assertTrue(SHORT_TOKEN_REGEX.match(token))

    async def test_create_and_populate_table(self):
        def create_user_table(txn: LoggingTransaction):
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS users(
                name TEXT,
                password_hash TEXT,
                creation_ts BIGINT UNSIGNED,
                admin BOOL DEFAULT 0 NOT NULL,
                UNIQUE(name)
                );
                """,
                (),
            )
            txn.execute(
                """
                INSERT INTO users VALUES (
                    "@jane.doe-synapse.org:dev01.synapse.org",
                    "$2b$12$58YVjKsB58DM.YFChWQM8uP0x3rh1iaKDNSPl3Jv34LGqwn7tIure",
                    1700823133,
                    0
                );
                """,
                (),
            )
            txn.execute(
                """
                INSERT INTO users VALUES (
                    "@john.doe-synapse.org:dev01.synapse.org",
                    "$2b$12$58YVjKsB58DM.YFChWQM8uP0x3rh1iaKDNSPl3Jv34LGqwn7tIure",
                    1700823160,
                    0
                );
                """,
                (),
            )
            txn.execute(
                """
                CREATE TABLE account_validity (
                    user_id text NOT NULL,
                    expiration_ts_ms bigint NOT NULL,
                    email_sent boolean NOT NULL,
                    renewal_token text,
                    token_used_ts_ms bigint
                );
                """,
                (),
            )
            txn.execute(
                """
                INSERT INTO account_validity VALUES (
                    "@john.doe-synapse.org:dev01.synapse.org",
                    1700823100,
                    false,
                    "mytoken",
                    1700823160
                );
                """,
                (),
            )

        populate_users = True
        module = await create_account_validity_module()
        await module._store._api.run_db_interaction("create_user_table", create_user_table, )

        await module._store.create_and_populate_table(populate_users)

        def check_email_account_validity(txn: LoggingTransaction):
            txn.execute("SELECT user_id FROM email_account_validity", ())
            return txn.fetchall()

        def check_email_status_account_validity(txn: LoggingTransaction):
            txn.execute("SELECT user_id FROM email_status_account_validity", ())
            return txn.fetchall()

        res = await module._store._api.run_db_interaction("", check_email_account_validity, )
        self.assertEqual(2, len(res))

        res = await module._store._api.run_db_interaction("", check_email_status_account_validity, )
        self.assertEqual(6, len(res))

    async def test_get_users_expiring_soon(self):
        module = await create_account_validity_module()
        now_ms = int(time.time() * 1000)

        user_id = "@izzy:test"
        user_id_date = now_ms + (6 * 24 * 60 * 60 * 1000)  # 6 days ahead
        await module.renew_account_for_user(
            user_id=user_id,
            expiration_ts=user_id_date,
        )
        user_id2 = "@joe:test"
        user_id_date2 = now_ms + (14 * 24 * 60 * 60 * 1000)  # 14 days ahead
        await module.renew_account_for_user(
            user_id=user_id2,
            expiration_ts=user_id_date2,
        )
        user_id3 = "@albert:test"
        user_id_date3 = now_ms + (60 * 24 * 60 * 60 * 1000)  # 60 days ahead
        await module.renew_account_for_user(
            user_id=user_id3,
            expiration_ts=user_id_date3,
        )

        expiring_users = await module._store.get_users_expiring_soon()

        self.assertEqual(2, len(expiring_users))

    async def test_send_renewal_email(self):
        # conf :
        # "period": "6w",
        # "send_renewal_email_at": ["30d", "2w", "1w"],
        module = await create_account_validity_module()
        now_ms = int(time.time() * 1000)

        threepids = {
            "@izzy:test": [{
                "medium": "email",
                "address": "izzy@test",
            }],
            "@joe:test": [{
                "medium": "email",
                "address": "joe@test",
            }],
            "@albert:test": [{
                "medium": "email",
                "address": "albert@test",
            }],
        }

        async def get_threepids(user_id):
            return threepids[user_id]

        module._api.get_threepids_for_user.side_effect = get_threepids

        user_id1 = "@izzy:test"
        user_id_date1 = now_ms + (6 * 24 * 60 * 60 * 1000)  # will expire 6 days
        await module.renew_account_for_user(
            user_id=user_id1,
            expiration_ts=user_id_date1,
        )
        user_id2 = "@joe:test"
        user_id_date2 = now_ms + (14 * 24 * 60 * 60 * 1000)  # will expire 14 days
        await module.renew_account_for_user(
            user_id=user_id2,
            expiration_ts=user_id_date2,
        )
        user_id3 = "@albert:test"
        user_id_date3 = now_ms + (60 * 24 * 60 * 60 * 1000)  # will expire 60 days
        await module.renew_account_for_user(
            user_id=user_id3,
            expiration_ts=user_id_date3,
        )

        # user_1 and user_id2 are the users that will expire soon : period 30d
        expiring_users = await module._store.get_users_expiring_soon()
        self.assertEqual(2, len(expiring_users))
        user_1 = expiring_users[0]
        self.assertEqual(user_id1, user_1[0])
        self.assertEqual(user_id_date1, user_1[1])
        self.assertEqual(parse_duration("30d"), user_1[2])
        user_2 = expiring_users[1]
        self.assertEqual(user_id2, user_2[0])
        self.assertEqual(user_id_date2, user_2[1])
        self.assertEqual(parse_duration("30d"), user_2[2])

        # should send a first renewal mail to 2 users : user_1 and user_id2 for period 30d
        await module._send_renewal_emails()

        # after first renewal email is sent, user_1 and user_id2 are the users that will expire soon : period 2 weeks
        expiring_users = await module._store.get_users_expiring_soon()
        self.assertEqual(2, len(expiring_users))
        user_1 = expiring_users[0]
        self.assertEqual(user_id1, user_1[0])
        self.assertEqual(user_id_date1, user_1[1])
        self.assertEqual(parse_duration("2w"), user_1[2])
        user_2 = expiring_users[1]
        self.assertEqual(user_id2, user_2[0])
        self.assertEqual(user_id_date2, user_2[1])
        self.assertEqual(parse_duration("2w"), user_2[2])

        # should send a second renewal mail to 2 users : user_1 and user_id2 for period 2 weeks
        await module._send_renewal_emails()

        # after second renewal email is sent, user_1 is the user that will expire soon : period 1 week
        expiring_users = await module._store.get_users_expiring_soon()
        self.assertEqual(1, len(expiring_users))
        user_1 = expiring_users[0]
        self.assertEqual(user_id1, user_1[0])
        self.assertEqual(user_id_date1, user_1[1])
        self.assertEqual(parse_duration("1w"), user_1[2])

        # should send a third renewal mail to user_1 for period 1 week
        await module._send_renewal_emails()

        # after third renewal email is sent, no user should remain as all email have been sent
        expiring_users = await module._store.get_users_expiring_soon()
        self.assertEqual(0, len(expiring_users))
        
    async def test_calculate_days_until_expiration(self):

        module = await create_account_validity_module()
        # Set the current time to 1 day before the expiration time
        expiration_ts = int(time.time() * 1000) + (86400 * 1000)
        self.assertEqual(module.calculate_days_until_expiration(expiration_ts), 1)

        # Set the current time to 2 days after the expiration time
        expiration_ts = int(time.time() * 1000) - (86400 * 2 * 1000)
        self.assertEqual(module.calculate_days_until_expiration(expiration_ts), 0)

        # Set the current time to exactly the expiration time
        expiration_ts = int(time.time() * 1000)
        self.assertEqual(module.calculate_days_until_expiration(expiration_ts), 0)

        # Set the expiration time to 0, which should result in 0 days until expiration
        expiration_ts = 0
        self.assertEqual(module.calculate_days_until_expiration(expiration_ts), 0)

        # Set the expiration time to a negative value, which should result in 0 days until expiration
        expiration_ts = -1000
        self.assertEqual(module.calculate_days_until_expiration(expiration_ts), 0)
