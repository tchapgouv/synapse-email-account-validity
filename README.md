# Synapse email account validity

A Synapse plugin module to manage account validity using validation emails.

After a configured time, this module automatically expires user accounts. When a user is
expired, Synapse will respond to any request authenticated by the user's access token
with a 403 error containing the `ORG_MATRIX_EXPIRED_ACCOUNT` error code. Some time before
an account expires (defined by the module's configuration), an email is sent to the user
with instructions to renew the validity of their account.

This module requires:

* Synapse >= 1.39.0
* sqlite3 >= 3.24.0 (if using SQLite with Synapse (not recommended))

## Installation

Thi plugin can be installed via PyPI:

```
pip install synapse-email-account-validity
```

## Config

Add the following in your Synapse config:

```yaml
modules:
  - module: email_account_validity.EmailAccountValidity
    config:
      # The maximum amount of time an account can stay valid for without being renewed.
      period: 6w
      # How long before an account expires should Synapse send it a renewal email (can send several renewal email).
      send_renewal_email_at: ["4w", "1w", "1d"]
      # Whether to include a link to click in the emails sent to users. If false, only a
      # renewal token is sent, in which case a shorter token is used, and the
      # user will need to copy it into a compatible client that will send an
      # authenticated request to the server.
      # Defaults to true.
      send_links: true
      # Overrides the subject of the renewal email
      renewal_email_subject: "Renew Your Tchap Account"
      # Deactivate this module for user_id containing this list of patterns
      exclude_user_id_patterns: 
        - "-test1.test4.org"
        - "-test1.test2.org"
      # How long do we wait after the expiration date to deactivate an account  
      deactivate_expired_account_period: 120d
```

The syntax for durations is the same as in the rest of Synapse's configuration file.

## Templates

The templates the module will use are:

* `notice_expiry.(html|txt)`: The content of the renewal email. It gets passed the
  following variables:
    * `app_name`: The value configured for `app_name` in the Synapse configuration file
      (under the `email` section).
    * `display_name`: The display name of the user needing renewal.
    * `expiration_ts`: A timestamp in milliseconds representing when the account will
      expire. Templates can use the `format_ts` (with a date format as the function's
      parameter) to format this timestamp into a human-readable date.
    * `url`: The URL the user is supposed to click on to renew their account. If
      `send_links` is set to `false` in the module's configuration, the value of this
      variable will be `None`.
    * `renewal_token`: The token to use in order to renew the user's account. If
      `send_links` is set to `false`, templates should prefer this variable to `url`.
    * `nb_days_before_expiracy`: Display number of days before expiration 
* `account_renewed.html`: The HTML to display to a user when they successfully renew
  their account. It gets passed the following vaiables:
    * `expiration_ts`: A timestamp in milliseconds representing when the account will
      expire. Templates can use the `format_ts` (with a date format as the function's
      parameter) to format this timestamp into a human-readable date.
* `account_previously_renewed.html`: The HTML to display to a user when they try to renew
  their account with a token that's valid but previously used. It gets passed the same
  variables as `account_renewed.html`.
* `invalid_token.html`: The HTML to display to a user when they try to renew their account
  with the wrong token. It doesn't get passed any variable.

You can find and change the default templates [here](https://github.com/matrix-org/synapse-email-account-validity/tree/main/email_account_validity/templates).
Admins can install custom templates either by changing the default ones directly, or by
configuring Synapse with a custom template directory that contains the custom templates.
Note that the templates directory contains two files that aren't templates (`mail.css`
and `mail-expiry.css`), but are used by email templates to apply visual adjustments.

Admins that don't need to customise their templates can just use the module as is and
ignore the previous paragraph.

## Routes

This plugin exposes three HTTP routes to manage account validity:

* `POST /_synapse/client/email_account_validity/send_mail`, which any registered user can
  hit with an access token to request a renewal email to be sent to their email addresses.
* `GET /_synapse/client/email_account_validity/renew`, which requires a `token` query
  parameter containing the latest token sent via email to the user, which renews the
  account associated with the token.
* `POST /_synapse/client/email_account_validity/admin`, which any server admin can use to
  manage the account validity of any registered user. It takes a JSON body with the
  following keys:
    * `user_id` (string, required): The Matrix ID of the user to update.
    * `expiration_ts` (integer, optional): The new expiration timestamp for this user, in
      milliseconds. If no token is provided, a value corresponding to `now + period` is
      used.
    * `enable_renewal_emails` (boolean, optional): Whether to allow renewal emails to be
      sent to this user.

The two first routes need to be reachable by the end users for this feature to work as
intended.

## Development and Testing

This repository uses `tox` to run tests.

### Tests

This repository uses `unittest` to run the tests located in the `tests`
directory. They can be ran with `tox -e tests`.

### Making a release

```
git tag vX.Y
python3 setup.py sdist
twine upload dist/synapse-email-account-validity-X.Y.tar.gz
git push origin vX.Y
```
