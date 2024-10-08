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

import logging
import random
import time
from typing import Dict, List, Optional, Tuple, Union

from synapse.module_api import DatabasePool, LoggingTransaction, ModuleApi, cached
from synapse.module_api.errors import SynapseError

from email_account_validity._config import EmailAccountValidityConfig
from email_account_validity._utils import TokenFormat

logger = logging.getLogger(__name__)

# The name of the column to look at for each type of renewal token.
_TOKEN_COLUMN_NAME = {
    TokenFormat.LONG: "long_renewal_token",
    TokenFormat.SHORT: "short_renewal_token",
}


class EmailAccountValidityStore:
    def __init__(self, config: EmailAccountValidityConfig, api: ModuleApi):
        self._api = api
        self._period = config.period
        self._exclude_user_id_patterns = config.exclude_user_id_patterns
        self._send_renewal_email_at = config.send_renewal_email_at
        self._expiration_ts_max_delta = self._period * 10.0 / 100.0
        self._rand = random.SystemRandom()
        self._deactivate_expired_account_period = config.deactivate_expired_account_period

        self._api.register_cached_function(self.get_expiration_ts_for_user)

    async def create_and_populate_table(self, populate_users: bool = True):
        """Create the email_account_validity table and populate it from other tables from
        within Synapse. It populates users in it by batches of 100 in order not to clog up
        the database connection with big requests.
        """

        def create_table_txn(txn: LoggingTransaction):
            # Try to create a table for the module.

            # The table we create has the following columns:
            #
            # * user_id: The user's Matrix ID.
            # * expiration_ts_ms: The expiration timestamp for this user in milliseconds.
            # * email_sent: Whether a renewal email has already been sent to this user
            # * long_renewal_token: Long renewal tokens, which are unique to the whole
            #                       table, so that renewing an account using one doesn't
            #                       require further authentication.
            # * short_renewal_token: Short renewal tokens, which aren't unique to the
            #                        whole table, and with which renewing an account
            #                        requires authentication using an access token.
            # * token_used_ts_ms: Timestamp at which the renewal token for the user has
            #                     been used, or NULL if it hasn't been used yet.

            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS email_account_validity(
                    user_id TEXT PRIMARY KEY,
                    expiration_ts_ms BIGINT NOT NULL,
                    long_renewal_token TEXT,
                    short_renewal_token TEXT,
                    token_used_ts_ms BIGINT
                )
                """,
                (),
            )

            txn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS long_renewal_token_idx
                    ON email_account_validity(long_renewal_token)
                """,
                (),
            )

            txn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS short_renewal_token_idx
                    ON email_account_validity(short_renewal_token, user_id)
                """,
                (),
            )

            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS email_status_account_validity(
                    user_id TEXT,
                    renewal_period_in_ts BIGINT,
                    email_sent BOOLEAN NOT NULL,
                    CONSTRAINT email_status_account_validity_pkey PRIMARY KEY (user_id,renewal_period_in_ts)
                )
                """,
                (),
            )

        def populate_table_txn(txn: LoggingTransaction, batch_size: int) -> int:
            # Populate the database with the users that are in the users table but not in
            # the email_account_validity one.
            sql_users = """
                SELECT users.name FROM users
                LEFT JOIN email_account_validity
                    ON (users.name = email_account_validity.user_id)
                WHERE email_account_validity.user_id IS NULL
                AND users.deactivated = 0
                """

            sql_args = []
            if self._exclude_user_id_patterns and len(self._exclude_user_id_patterns) > 0:
                for k in self._exclude_user_id_patterns:
                    sql_users += f" AND users.name NOT LIKE ?"
                    sql_args.append(f"%{k}%")
            sql_users += " LIMIT ?"
            sql_args.append(batch_size)

            txn.execute(
                sql_users,
                tuple(sql_args),
            )

            missing_users = txn.fetchall()
            if not missing_users:
                return 0

            # Turn the results into a dictionary so we can later merge it with the list
            # of registered users on the homeserver.
            users_to_insert = {}

            # Look for users that are registered but don't have a state in the
            # account_validity table, and set a default state for them. This default
            # state includes an expiration timestamp close to now + validity period, but
            # is slightly randomised to avoid sending huge bursts of renewal emails at
            # once.
            default_expiration_ts = int(time.time() * 1000) + self._period
            for user in missing_users:
                if users_to_insert.get(user[0]) is None:
                    users_to_insert[user[0]] = {
                        "user_id": user[0],
                        "expiration_ts_ms": self._rand.uniform(
                            default_expiration_ts - self._expiration_ts_max_delta,
                            default_expiration_ts,
                        ),
                        "email_sent": False,
                        "renewal_token": None,
                        "token_used_ts_ms": None,
                    }

            # Insert the users in the email_account_validity table.
            DatabasePool.simple_insert_many_txn(
                txn=txn,
                table="email_account_validity",
                keys=[
                    "user_id",
                    "expiration_ts_ms",
                    "long_renewal_token",
                    "token_used_ts_ms",
                ],
                values=[
                    (
                        user["user_id"],
                        user["expiration_ts_ms"],
                        # If there's a renewal token for the user, we consider it's a long
                        # one, because the non-module implementation of account validity
                        # doesn't have a concept of short tokens.
                        user["renewal_token"],
                        user["token_used_ts_ms"],
                    )
                    for user in users_to_insert.values()
                ],
            )

            users_period_to_insert = {}
            for renewal_period_in_ts in self._send_renewal_email_at:
                for user in users_to_insert.values():
                    users_period_to_insert[f"{user['user_id']}_{renewal_period_in_ts}"] = {
                        "user_id": user["user_id"],
                        "renewal_period_in_ts": renewal_period_in_ts,
                        "email_sent": user["email_sent"]
                    }
            # Insert the users in the table.
            DatabasePool.simple_insert_many_txn(
                txn=txn,
                table="email_status_account_validity",
                keys=[
                    "user_id",
                    "renewal_period_in_ts",
                    "email_sent"
                ],
                values=[
                    (
                        user["user_id"],
                        user["renewal_period_in_ts"],
                        user["email_sent"]
                    )
                    for user in users_period_to_insert.values()
                ],
            )

            return len(missing_users)

        def delete_email_account_validity_txn(txn: LoggingTransaction):
            sql_args = [1, 0]
            where_clauses = " WHERE ?=?"
            if self._exclude_user_id_patterns and len(self._exclude_user_id_patterns) > 0:
                for k in self._exclude_user_id_patterns:
                    where_clauses += f" OR user_id LIKE ?"
                    sql_args.append(f"%{k}%")

                sql_stmt = """
                    DELETE FROM email_status_account_validity
                """ + where_clauses

                txn.execute(
                    sql_stmt,
                    tuple(sql_args),
                )

                sql_stmt = """
                    DELETE FROM email_account_validity
                """ + where_clauses
                txn.execute(
                    sql_stmt,
                    tuple(sql_args),
                )

        await self._api.run_db_interaction(
            "account_validity_create_table",
            create_table_txn,
        )

        await self._api.run_db_interaction(
            "delete_email_account_validity_for_user_txn",
            delete_email_account_validity_txn
        )

        if populate_users:
            batch_size = 100
            processed_rows = 100
            while processed_rows == batch_size:
                processed_rows = await self._api.run_db_interaction(
                    "account_validity_populate_table",
                    populate_table_txn,
                    batch_size,
                )
                logger.info(
                    "Inserted %s users in the email account validity table",
                    processed_rows,
                )
        logger.info("Creation and population of the email account validity tables is now completed.")

    async def get_users_expiring_soon(self) -> List[Dict[str, Union[str, int]]]:
        """Selects users whose account will expire in the [now, now + send_renewal_email_at] time
        window (see configuration for account_validity for information on what send_renewal_email_at
        refers to).

        Returns:
            A list of dictionaries, each with a user ID and expiration time (in
            milliseconds).
        """

        def select_users_txn(txn):
            now_ms = int(time.time() * 1000)

            txn.execute(
                """
                SELECT eav.user_id, eav.expiration_ts_ms, MAX(esav.renewal_period_in_ts) as closest_renewal_period
                FROM email_account_validity eav
                JOIN email_status_account_validity esav
                ON eav.user_id = esav.user_id AND esav.email_sent = ?
                GROUP BY eav.user_id, eav.expiration_ts_ms
                HAVING (eav.expiration_ts_ms - ?) <= MAX(esav.renewal_period_in_ts)
                """,
                (False, now_ms),
            )
            return txn.fetchall()

        return await self._api.run_db_interaction(
            "get_users_expiring_soon",
            select_users_txn
        )

    async def get_expired_users(self) -> List[Dict[str, Union[str, int]]]:
        """Selects users whose account has been expired since `deactivate_expired_account_period`.

        Returns:
            A list of dictionaries, each with a user ID and expiration time (in
            milliseconds).
        """

        def select_users_txn(txn):
            now_ms = int(time.time() * 1000)

            txn.execute(
                """
                SELECT eav.user_id
                FROM email_account_validity eav
                JOIN users u ON u.name = eav.user_id
                WHERE eav.expiration_ts_ms + ? <=  ?
                AND u.deactivated = 0
                LIMIT 1000
                """,
                (self._deactivate_expired_account_period, now_ms),
            )
            return txn.fetchall()

        return await self._api.run_db_interaction(
            "get_expired_users",
            select_users_txn
        )

    async def set_account_validity_for_user(
            self,
            user_id: str,
            expiration_ts: int,
            email_sent: bool,
            send_renewal_email_at: List[int],
            token_format: TokenFormat,
            renewal_token: Optional[str] = None,
            token_used_ts: Optional[int] = None,
    ):
        """Updates the account validity properties of the given account, with the
        given values.

        Args:
            user_id: ID of the account to update properties for.
            expiration_ts: New expiration date, as a timestamp in milliseconds
                since epoch.
            email_sent: True means a renewal email has been sent for this account
                and there's no need to send another one for the current validity
                period.
            send_renewal_email_at: List of period to know when to send the renewal mail
            token_format: The configured token format, used to determine which
                column to update.
            renewal_token: Renewal token the user can use to extend the validity
                of their account. Defaults to no token.
            token_used_ts: A timestamp of when the current token was used to renew
                the account.
        """

        def set_account_validity_for_user_txn(txn: LoggingTransaction):
            txn.execute(
                """
                INSERT INTO email_account_validity (
                    user_id,
                    expiration_ts_ms,
                    %(token_column_name)s,
                    token_used_ts_ms
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE
                SET
                    expiration_ts_ms = EXCLUDED.expiration_ts_ms,
                    %(token_column_name)s = EXCLUDED.%(token_column_name)s,
                    token_used_ts_ms = EXCLUDED.token_used_ts_ms
                """ % {"token_column_name": _TOKEN_COLUMN_NAME[token_format]},
                (user_id, expiration_ts, renewal_token, token_used_ts)
            )

        await self._api.run_db_interaction(
            "set_account_validity_for_user",
            set_account_validity_for_user_txn,
        )

        def set_account_status_validity_for_user_txn(txn: LoggingTransaction):
            txn.execute(
                """
                DELETE FROM email_status_account_validity 
                WHERE user_id = ?
                """,
                (user_id,)
            )
            for renewal_period_in_ts in send_renewal_email_at:
                txn.execute(
                    """
                    INSERT INTO email_status_account_validity (
                        user_id,
                        renewal_period_in_ts,
                        email_sent
                    )
                    VALUES (?, ?, ?)
                    """,
                    (user_id, renewal_period_in_ts, email_sent)
                )

        await self._api.run_db_interaction(
            "set_account_validity_for_user",
            set_account_status_validity_for_user_txn,
        )

        await self._api.invalidate_cache(self.get_expiration_ts_for_user, (user_id,))

    @cached()
    async def get_expiration_ts_for_user(self, user_id: str) -> int:
        """Get the expiration timestamp for the account bearing a given user ID.

        Args:
            user_id: The ID of the user.
        Returns:
            None, if the account has no expiration timestamp, otherwise int
            representation of the timestamp (as a number of milliseconds since epoch).
        """

        def get_expiration_ts_for_user_txn(txn: LoggingTransaction):
            return DatabasePool.simple_select_one_onecol_txn(
                txn=txn,
                table="email_account_validity",
                keyvalues={"user_id": user_id},
                retcol="expiration_ts_ms",
                allow_none=True,
            )

        res = await self._api.run_db_interaction(
            "get_expiration_ts_for_user",
            get_expiration_ts_for_user_txn,
        )
        return res

    async def set_renewal_token_for_user(
            self,
            user_id: str,
            renewal_token: str,
            token_format: TokenFormat,
    ):
        """Store the given renewal token for the given user.

        Args:
            user_id: The user ID to store the renewal token for.
            renewal_token: The renewal token to store for the user.
            token_format: The configured token format, used to determine which
                column to update.
        """

        def set_renewal_token_for_user_txn(txn: LoggingTransaction):
            # We don't need to check if the token is unique since we've got unique
            # indexes to check that.
            try:
                DatabasePool.simple_update_one_txn(
                    txn=txn,
                    table="email_account_validity",
                    keyvalues={"user_id": user_id},
                    updatevalues={
                        _TOKEN_COLUMN_NAME[token_format]: renewal_token,
                        "token_used_ts_ms": None,
                    },
                )
            except Exception:
                raise SynapseError(500, "Failed to update renewal token")

        await self._api.run_db_interaction(
            "set_renewal_token_for_user",
            set_renewal_token_for_user_txn,
        )

    async def validate_renewal_token(
            self,
            renewal_token: str,
            token_format: TokenFormat,
            user_id: Optional[str] = None,
    ) -> Tuple[str, int, Optional[int]]:
        """Check if the provided renewal token is associating with a user, optionally
        validating the user it belongs to as well, and return the account renewal status
        of the user it belongs to.

        Args:
            renewal_token: The renewal token to perform the lookup with.
            token_format: The configured token format, used to determine which
                column to update.
            user_id: The Matrix ID of the user to renew, if the renewal request was
                authenticated.

        Returns:
            A tuple of containing the following values:
                * The ID of a user to which the token belongs.
                * An int representing the user's expiry timestamp as milliseconds since
                    the epoch, or 0 if the token was invalid.
                * An optional int representing the timestamp of when the user renewed
                    their account timestamp as milliseconds since the epoch. None if the
                    account has not been renewed using the current token yet.

        Raises:
            StoreError(404): The token could not be found (or does not belong to the
                provided user, if any).
        """

        def get_user_from_renewal_token_txn(txn: LoggingTransaction):
            keyvalues = {_TOKEN_COLUMN_NAME[token_format]: renewal_token}
            if user_id is not None:
                keyvalues["user_id"] = user_id

            return DatabasePool.simple_select_one_txn(
                txn=txn,
                table="email_account_validity",
                keyvalues=keyvalues,
                retcols=["user_id", "expiration_ts_ms", "token_used_ts_ms"],
            )

        res = await self._api.run_db_interaction(
            "get_user_from_renewal_token",
            get_user_from_renewal_token_txn,
        )

        return res[0], res[1], res[2]

    async def set_expiration_date_for_user(self, user_id: str):
        """Sets an expiration date to the account with the given user ID.

        Args:
             user_id: User ID to set an expiration date for.
        """
        await self._api.run_db_interaction(
            "set_expiration_date_for_user",
            self.set_expiration_date_for_user_txn,
            user_id,
        )

    def set_expiration_date_for_user_txn(
            self,
            txn: LoggingTransaction,
            user_id: str,
    ):
        """Sets an expiration date to the account with the given user ID.

        Args:
            user_id: User ID to set an expiration date for.
        """
        now_ms = int(time.time() * 1000)
        expiration_ts = now_ms + self._period

        sql = """
        INSERT INTO email_account_validity (user_id, expiration_ts_ms)
        VALUES (?, ?)
        ON CONFLICT (user_id) DO
            UPDATE SET
                expiration_ts_ms = EXCLUDED.expiration_ts_ms
        """

        txn.execute(sql, (user_id, expiration_ts))

        txn.execute(
                """
                DELETE FROM email_status_account_validity 
                WHERE user_id = ?
                """,
                (user_id,)
            )

        sql = """
                INSERT INTO email_status_account_validity (
                    user_id,
                    renewal_period_in_ts,
                    email_sent
                )
                VALUES (?, ?, ?)
        """

        for renewal_period_in_ts in self._send_renewal_email_at:
            txn.execute(sql, (user_id, renewal_period_in_ts, False))

        txn.call_after(self.get_expiration_ts_for_user.invalidate, (user_id,))

    async def set_renewal_mail_status(self, user_id: str, renewal_period_in_ts: int, email_sent: bool) -> None:
        """Sets or unsets the flag that indicates whether a renewal email has been sent
        to the user (and the user hasn't renewed their account yet).

        Args:
            user_id: ID of the user to set/unset the flag for.
            renewal_period_in_ts: renewal period to set/unset the flag for.
            email_sent: Flag which indicates whether a renewal email has been sent
                to this user.
        """

        def set_renewal_mail_status_txn(txn: LoggingTransaction):
            DatabasePool.simple_update_one_txn(
                txn=txn,
                table="email_status_account_validity",
                keyvalues={"user_id": user_id, "renewal_period_in_ts": renewal_period_in_ts},
                updatevalues={"email_sent": email_sent},
            )

        await self._api.run_db_interaction(
            "set_renewal_mail_status",
            set_renewal_mail_status_txn,
        )

    async def get_renewal_token_for_user(
            self,
            user_id: str,
            token_format: TokenFormat,
    ) -> str:
        """Retrieve the renewal token for the given user.

        Args:
            user_id: Matrix ID of the user to retrieve the renewal token of.
            token_format: The configured token format, used to determine which
                column to update.

        Returns:
            The renewal token for the user.
        """

        def get_renewal_token_txn(txn: LoggingTransaction):
            return DatabasePool.simple_select_one_onecol_txn(
                txn=txn,
                table="email_account_validity",
                keyvalues={"user_id": user_id},
                retcol=_TOKEN_COLUMN_NAME[token_format],
            )

        return await self._api.run_db_interaction(
            "get_renewal_token_for_user",
            get_renewal_token_txn,
        )

    async def deactivate_account_validity_for_user(self, user_id: str) -> None:
        def delete_email_account_validity_for_user_txn(
            txn: LoggingTransaction,
            user_id: str
        ):
            txn.execute(
                """
                DELETE FROM email_status_account_validity 
                WHERE user_id = ?
                """,
                (user_id,)
            )
            txn.execute(
                """
                DELETE FROM email_account_validity 
                WHERE user_id = ?
                """,
                (user_id,)
            )
            txn.call_after(self.get_expiration_ts_for_user.invalidate, (user_id,))

        await self._api.run_db_interaction(
            "delete_email_account_validity_for_user_txn",
            delete_email_account_validity_for_user_txn,
            user_id,
        )

