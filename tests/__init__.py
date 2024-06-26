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

from collections import namedtuple
import sqlite3
import time
from unittest import mock

import jinja2
from synapse.module_api import ModuleApi
from synapse.storage.engines import create_engine, BaseDatabaseEngine

from email_account_validity import EmailAccountValidity


class SQLiteStore:
    """In-memory SQLite store. We can't just use a run_db_interaction function that opens
    its own connection, since we need to use the same connection for all queries in a
    test.
    """
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")

    async def run_db_interaction(self, desc, f, *args, **kwargs):
        db_config = {"name": "sqlite3"}
        engine = create_engine(db_config)
        cur = CursorWrapper(self.conn.cursor(), engine)
        try:
            res = f(cur, *args, **kwargs)
            self.conn.commit()
            return res
        except Exception:
            self.conn.rollback()
            raise


class CursorWrapper:
    """Wrapper around a SQLite cursor that also provides a call_after method."""
    def __init__(self, cursor: sqlite3.Cursor, engine: BaseDatabaseEngine):
        self.cur = cursor
        self.database_engine = engine

    def execute(self, sql, args):
        self.cur.execute(sql, args)

    @property
    def rowcount(self):
        return self.cur.rowcount

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def call_after(self, f, args):
        f(args)

    def execute_batch(self, sql, args):
        self.cur.executemany(sql, args)

    def __iter__(self):
        return self.cur.__iter__()

    def __next__(self):
        return self.cur.__next__()


def read_templates(filenames, directory):
    """Reads Jinja templates from the templates directory. This function is mostly copied
    from Synapse.
    """
    loader = jinja2.FileSystemLoader(directory)
    env = jinja2.Environment(
        loader=loader,
        autoescape=jinja2.select_autoescape(),
    )

    def _format_ts_filter(value: int, format: str):
        return time.strftime(format, time.localtime(value / 1000))

    env.filters.update(
        {
            "format_ts": _format_ts_filter,
        }
    )

    return [env.get_template(filename) for filename in filenames]


async def get_profile_for_user(user_id):
    ProfileInfo = namedtuple("ProfileInfo", ("avatar_url", "display_name"))
    return ProfileInfo(None, "Izzy")


async def send_mail(recipient, subject, html, text):
    return None


async def invalidate_cache(cached_func, keys):
    cached_func.invalidate(keys)


async def create_account_validity_module(config={}) -> EmailAccountValidity:
    """Starts an EmailAccountValidity module with a basic config and a mock of the
    ModuleApi.
    """
    config.update(
        {
            "period": "6w",
            "send_renewal_email_at": ["30d", "2w", "1w"],
            "exclude_user_id_patterns": ["-test1.test4.org", "-test1.test2.org"]
        }
    )

    store = SQLiteStore()

    # Create a mock based on the ModuleApi spec, but override some mocked functions
    # because some capabilities (interacting with the database, getting the current time,
    # etc.) are needed for running the tests.
    module_api = mock.Mock(spec=ModuleApi)
    module_api.run_db_interaction.side_effect = store.run_db_interaction
    module_api.read_templates.side_effect = read_templates
    module_api.get_profile_for_user.side_effect = get_profile_for_user
    module_api.send_mail.side_effect = send_mail
    module_api.invalidate_cache.side_effect = invalidate_cache

    # Make sure the table is created. Don't try to populate with users since we don't
    # have tables to populate from.
    parsed_config = EmailAccountValidity.parse_config(config)
    module = EmailAccountValidity(parsed_config, module_api, populate_users=False)
    await module._store.create_and_populate_table(populate_users=False)

    return module
