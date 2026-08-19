"""Order-independent invariant: every test starts with clean singletons.

The ``process_manager`` and ``config_manager`` singletons survive the
whole pytest session. The autouse ``_clean_process_manager`` fixture (see
conftest.py) resets them around every test and roots their persistent
paths in that test's ``tmp_path``. This test pins the guarantee: whatever
test ran before, at the start of any test the singletons are empty and
nowhere near the repo's ``apps/solar-host/logs`` / ``config.json``.
"""

from solar_host.config import config_manager
from solar_host.process_manager import process_manager


def test_singletons_clean_and_tmp_rooted_at_test_start(tmp_path):
    # The singleton's log_dir must point inside this test's tmp_path, not
    # the repo's apps/solar-host/logs (cached at import time).
    assert process_manager.log_dir.is_relative_to(tmp_path)
    # config_manager writes to this test's tmp_path, not the repo's
    # gitignored config.json.
    assert config_manager.config_file.is_relative_to(tmp_path)

    assert process_manager.processes == {}
    assert process_manager.log_buffers == {}
    assert process_manager.log_sequences == {}
    assert process_manager.last_exit_codes == {}

    assert config_manager.instances == {}
