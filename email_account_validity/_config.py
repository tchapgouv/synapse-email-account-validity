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
from typing import Optional, List

import attr


@attr.s(frozen=True, auto_attribs=True)
class EmailAccountValidityConfig:
    period: int
    send_renewal_email_at: List[int]
    renewal_email_subject: Optional[str] = None
    exclude_user_id_patterns: List[str] = []
    send_links: bool = True
    deactivate_expired_account_period: Optional[int] = None
