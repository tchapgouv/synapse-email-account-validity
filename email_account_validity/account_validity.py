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
import time
from typing import Tuple, Optional

from twisted.web.server import Request

from synapse.module_api import ModuleApi, run_in_background
from synapse.module_api.errors import ConfigError

from email_account_validity._base import EmailAccountValidityBase
from email_account_validity._config import EmailAccountValidityConfig
from email_account_validity._servlets import EmailAccountValidityServlet
from email_account_validity._store import EmailAccountValidityStore
from email_account_validity._utils import parse_duration

logger = logging.getLogger(__name__)


class EmailAccountValidity(EmailAccountValidityBase):
    def __init__(
        self,
        config: EmailAccountValidityConfig,
        api: ModuleApi,
        populate_users: bool = True,
    ):
        if not api.public_baseurl:
            raise ConfigError("Can't send renewal emails without 'public_baseurl'")

        self._store = EmailAccountValidityStore(config, api)
        self._api = api

        super().__init__(config, self._api, self._store)

        run_in_background(self._store.create_and_populate_table, populate_users)
        self._api.looping_background_call(
            self._send_renewal_emails, 30 * 60 * 1000
        )

        if config.deactivate_expired_account_period:
            if self._api.worker_name is None:
                self._api.looping_background_call(
                    self._deactivate_expired_account, 60 * 60 * 1000,
                    run_on_all_instances=True
                )
        else:
            logger.info("The feature to deactivate expired account is NOT enabled")

        self._api.register_account_validity_callbacks(
            is_user_expired=self.is_user_expired,
            on_user_registration=self.on_user_registration,
            on_legacy_send_mail=self.on_legacy_send_mail,
            on_legacy_renew=self.on_legacy_renew,
            on_legacy_admin_request=self.on_legacy_admin_request,
        )

        self._api.register_web_resource(
            path="/_synapse/client/email_account_validity",
            resource=EmailAccountValidityServlet(config, self._api, self._store)
        )

    @staticmethod
    def parse_config(config: dict):
        """Check that the configuration includes the required keys and parse the values
        expressed as durations."""
        if "period" not in config:
            raise ConfigError("'period' is required when using email account validity")

        if "send_renewal_email_at" not in config:
            raise ConfigError(
                "'send_renewal_email_at' is required when using email account validity"
            )

        send_renewal_email_at = [x.strip() for x in config["send_renewal_email_at"]]

        deactivate_expired_account_period = None
        if "deactivate_expired_account_period" in config:
            deactivate_expired_account_period = parse_duration(config["deactivate_expired_account_period"])

        parsed_config = EmailAccountValidityConfig(
            period=parse_duration(config["period"]),
            send_renewal_email_at=[parse_duration(x) for x in send_renewal_email_at],
            renewal_email_subject=config.get("renewal_email_subject"),
            exclude_user_id_patterns=config.get("exclude_user_id_patterns", []),
            send_links=config.get("send_links", True),
            deactivate_expired_account_period=deactivate_expired_account_period
        )
        return parsed_config

    async def on_legacy_renew(self, renewal_token: str) -> Tuple[bool, bool, int]:
        """Attempt to renew an account and return the results of this attempt to the
        deprecated /renew servlet.

        Args:
            renewal_token: Token sent with the renewal request.

        Returns:
            A tuple containing:
              * A bool representing whether the token is valid and unused.
              * A bool which is `True` if the token is valid, but stale.
              * An int representing the user's expiry timestamp as milliseconds since the
                epoch, or 0 if the token was invalid.
        """
        return await self.renew_account(renewal_token)

    async def on_legacy_send_mail(self, user_id: str):
        """Sends a renewal email to the addresses associated with the given Matrix user
        ID.

        Args:
            user_id: The user ID to send a renewal email for.
        """
        await self.send_renewal_email_to_user(user_id)

    async def on_legacy_admin_request(self, request: Request) -> int:
        """Update the account validity state of a user using the data from the given
        request.

        Args:
            request: The request to extract data from.

        Returns:
            The new expiration timestamp for the updated user.
        """
        return await self.set_account_validity_from_request(request)

    async def is_user_expired(self, user_id: str) -> Optional[bool]:
        """Checks whether a user is expired.

        Args:
            user_id: The user to check the expiration state for.

        Returns:
            A boolean indicating if the user has expired, or None if the module could not
            figure it out (i.e. if the user has no expiration timestamp).
        """
        if self.user_id_is_in_excluded_patterns(user_id):
            return None
        expiration_ts = await self._store.get_expiration_ts_for_user(user_id)
        if expiration_ts is None:
            return None

        now_ts = int(time.time() * 1000)
        return now_ts >= expiration_ts

    async def on_user_registration(self, user_id: str):
        """Set the expiration timestamp for a newly registered user.

        Args:
            user_id: The ID of the newly registered user to set an expiration date for.
        """
        if self.user_id_is_in_excluded_patterns(user_id):
            await self._store.deactivate_account_validity_for_user(user_id)
        else:
            await self._store.set_expiration_date_for_user(user_id)

    async def _send_renewal_emails(self):
        """Gets the list of users whose account is expiring in the amount of time
        configured in the ``send_renewal_email_at`` parameter from the ``account_validity``
        configuration, and sends renewal emails to all of these users as long as they
        have an email 3PID attached to their account.
        """
        logger.info("Sending renewal emails")
        expiring_users = await self._store.get_users_expiring_soon()

        logger.debug(f"Sending renewal emails to {len(expiring_users)} users")
        if expiring_users:
            for user in expiring_users:
                if user[1] is None:
                    logger.warning(
                        "User %s has no expiration ts, ignoring" % user["user_id"],
                    )
                    continue
                logger.debug(f"Sending renewal emails to user_id={user[0]}"
                             f", expiration_ts={user[1]}"
                             f", renewal_period_in_ts={user[2]}")
                await self.send_renewal_email(
                    user_id=user[0], expiration_ts=user[1], renewal_period_in_ts=user[2]
                )

    async def _deactivate_expired_account(self):
        """Deactivate users whose account has expired since ``deactivate_expired_account_period``
        """
        if self._api.worker_name is not None:
            return
        logger.info("Deactivating expired account")
        expired_users = await self._store.get_expired_users()
        nb_expired_users = len(expired_users)
        count = 0
        if expired_users:
            for user in expired_users:
                result = await self.deactivate_account(user_id=user[0])
                if result:
                    count = count + 1
                    logger.debug(f"{count}/{nb_expired_users} deactivated account - user_id={user[0]} - done")
        logger.info(f"Deactivate expired account - {count}/{nb_expired_users} users")