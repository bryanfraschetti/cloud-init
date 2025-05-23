"""Integration tests for the user_groups module.

TODO:
* This module assumes that the "ubuntu" user will be created when "default" is
  specified; this will need modification to run on other OSes.
"""

import re

import pytest

from tests.integration_tests.instances import IntegrationInstance
from tests.integration_tests.releases import (
    CURRENT_RELEASE,
    IS_UBUNTU,
    JAMMY,
    NOBLE,
)
from tests.integration_tests.util import verify_clean_boot

USER_DATA = """\
#cloud-config
# Add groups to the system
groups:
  - secret: [root]
  - cloud-users

# Add users to the system. Users are added after groups are added.
users:
  - default
  - name: foobar
    gecos: Foo B. Bar
    primary_group: foobar
    groups: users
    expiredate: '2038-01-19'
    lock_passwd: false
    passwd: $6$j212wezy$7H/1LT4f9/N3wpgNunhsIqtMj62OKiS3nyNwuizouQc3u7MbYCarYe\
AHWYPYb2FT.lbioDm2RrkJPb9BZMN1O/
  - name: barfoo
    gecos: Bar B. Foo
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    groups: [cloud-users, secret]
    lock_passwd: true
  - name: nopassworduser
    gecos: I do not like passwords
    lock_passwd: false
  - name: cloudy
    gecos: Magic Cloud App Daemon User
    inactive: '0'
    system: true
  - name: eric
    sudo: null
    uid: 1742
  - name: archivist
    uid: 1743
"""

NEW_USER_EMPTY_PASSWD_WARNING = "Not unlocking password for user {username}. 'lock_passwd: false' present in user-data but no 'passwd'/'plain_text_passwd'/'hashed_passwd' provided in user-data"  # noqa: E501

EXISTING_USER_EMPTY_PASSWD_WARNING = "Not unlocking blank password for existing user {username}. 'lock_passwd: false' present in user-data but no existing password set and no 'plain_text_passwd'/'hashed_passwd' provided in user-data"  # noqa E501


@pytest.mark.ci
@pytest.mark.user_data(USER_DATA)
class TestUsersGroups:
    """Test users and groups.

    This test specifies a number of users and groups via user-data, and
    confirms that they have been configured correctly in the system under test.
    """

    @pytest.mark.skipif(not IS_UBUNTU, reason="Test assumes 'ubuntu' user")
    @pytest.mark.parametrize(
        "getent_args,regex",
        [
            # Test the ubuntu group
            (["group", "ubuntu"], r"ubuntu:x:[0-9]{4}:"),
            # Test the cloud-users group
            (["group", "cloud-users"], r"cloud-users:x:[0-9]{4}:barfoo"),
            # Test the ubuntu user
            (
                ["passwd", "ubuntu"],
                r"ubuntu:x:[0-9]{4}:[0-9]{4}:Ubuntu:/home/ubuntu:/bin/bash",
            ),
            # Test the foobar user
            (
                ["passwd", "foobar"],
                r"foobar:x:[0-9]{4}:[0-9]{4}:Foo B. Bar:/home/foobar:",
            ),
            # Test the barfoo user
            (
                ["passwd", "barfoo"],
                r"barfoo:x:[0-9]{4}:[0-9]{4}:Bar B. Foo:/home/barfoo:",
            ),
            # Test the cloudy user
            (["passwd", "cloudy"], r"cloudy:x:[0-9]{3,4}:"),
            # Test str uid
            (["passwd", "eric"], r"eric:x:1742:"),
            # Test int uid
            (["passwd", "archivist"], r"archivist:x:1743:"),
            # Test int uid
            (
                ["passwd", "nopassworduser"],
                r"nopassworduser:x:[0-9]{4}:[0-9]{4}:I do not like passwords",
            ),
        ],
    )
    def test_users_groups(self, regex, getent_args, class_client):
        """Use getent to interrogate the various expected outcomes"""
        result = class_client.execute(["getent"] + getent_args)
        assert re.search(regex, result.stdout) is not None, (
            "'getent {}' resulted in '{}', "
            "but expected to match regex {}".format(
                " ".join(getent_args), result.stdout, regex
            )
        )

    def test_initial_warnings(self, class_client):
        """Check for initial warnings."""
        warnings = (
            [NEW_USER_EMPTY_PASSWD_WARNING.format(username="nopassworduser")]
            if CURRENT_RELEASE > NOBLE
            else []
        )
        verify_clean_boot(
            class_client,
            require_warnings=warnings,
        )

    def test_user_root_in_secret(self, class_client):
        """Test root user is in 'secret' group."""
        output = class_client.execute("groups root").stdout
        _, groups_str = output.split(":", maxsplit=1)
        groups = groups_str.split()
        assert "secret" in groups

    def test_nopassword_unlock_warnings(self, class_client):
        """Verify warnings for empty passwords for new and existing users."""
        # Fake admin clearing and unlocking and empty unlocked password foobar
        # This will generate additional warnings about not unlocking passwords
        # for pre-existing users which have an existing empty password
        class_client.execute("passwd -d foobar")
        class_client.instance.clean()
        class_client.restart()
        warnings = (
            [
                EXISTING_USER_EMPTY_PASSWD_WARNING.format(
                    username="nopassworduser"
                ),
                EXISTING_USER_EMPTY_PASSWD_WARNING.format(username="foobar"),
            ]
            if CURRENT_RELEASE > NOBLE
            else []
        )
        verify_clean_boot(
            class_client,
            ignore_warnings=True,  # ignore warnings about existing groups
            require_warnings=warnings,
        )


@pytest.mark.user_data(USER_DATA)
@pytest.mark.skipif(
    CURRENT_RELEASE < JAMMY,
    reason="Requires version of sudo not available in older releases",
)
def test_sudoers_includedir(client: IntegrationInstance):
    """Ensure we don't add additional #includedir to sudoers.

    Newer versions of /etc/sudoers will use @includedir rather than
    #includedir. Ensure we handle that properly and don't include an
    additional #includedir when one isn't warranted.

    https://github.com/canonical/cloud-init/pull/783
    """
    client.execute("sed -i 's/#include/@include/g' /etc/sudoers")

    sudoers_content_before = client.read_from_file(
        "/etc/sudoers.d/90-cloud-init-users"
    ).splitlines()[1:]
    sudoers = client.read_from_file("/etc/sudoers")
    if "@includedir /etc/sudoers.d" not in sudoers:
        client.execute("echo '@includedir /etc/sudoers.d' >> /etc/sudoers")
    client.instance.clean()
    client.restart()
    sudoers = client.read_from_file("/etc/sudoers")

    assert "#includedir" not in sudoers
    assert sudoers.count("includedir /etc/sudoers.d") == 1

    sudoers_content_after = client.read_from_file(
        "/etc/sudoers.d/90-cloud-init-users"
    ).splitlines()[1:]
    assert sudoers_content_before == sudoers_content_after
