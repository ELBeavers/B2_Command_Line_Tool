######################################################################
#
# File: test/integration/conftest.py
#
# Copyright 2020 Backblaze Inc. All Rights Reserved.
#
# License https://www.backblaze.com/using_b2_code.html
#
######################################################################
import sys

from os import environ, path
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

import pytest

from b2sdk.v2 import B2_ACCOUNT_INFO_ENV_VAR, XDG_CONFIG_HOME_ENV_VAR
from b2sdk.exception import BucketIdNotFound
from filelock import FileLock

from .helpers import Api, CommandLine, bucket_name_part

GENERAL_BUCKET_NAME_PREFIX = 'clitst'


@pytest.hookimpl
def pytest_addoption(parser):
    parser.addoption(
        '--sut', default='%s -m b2' % sys.executable, help='Path to the System Under Test'
    )
    parser.addoption('--cleanup', action='store_true', help='Perform full cleanup at exit')


@pytest.fixture(scope='session')
def application_key() -> str:
    key = environ.get('B2_TEST_APPLICATION_KEY')
    assert application_key, 'B2_TEST_APPLICATION_KEY is not set'
    yield key


@pytest.fixture(scope='session')
def application_key_id() -> str:
    key_id = environ.get('B2_TEST_APPLICATION_KEY_ID')
    assert key_id, 'B2_TEST_APPLICATION_KEY_ID is not set'
    yield key_id


@pytest.fixture(scope='session')
def realm() -> str:
    yield environ.get('B2_TEST_ENVIRONMENT', 'production')


@pytest.fixture(scope='function')
def bucket_name(b2_api) -> str:
    bucket = b2_api.create_bucket()
    yield bucket.name
    try:
        b2_api.clean_bucket(bucket)
    except BucketIdNotFound:
        pass


@pytest.fixture(scope='function')  # , autouse=True)
def debug_print_buckets(b2_api):
    print('-' * 30)
    print('Buckets before test ' + environ['PYTEST_CURRENT_TEST'])
    num_buckets = b2_api.count_and_print_buckets()
    print('-' * 30)
    try:
        yield
    finally:
        print('-' * 30)
        print('Buckets after test ' + environ['PYTEST_CURRENT_TEST'])
        delta = b2_api.print_buckets() - num_buckets
        print(f'DELTA: {delta}')
        print('-' * 30)


@pytest.fixture(scope='session')
def this_run_bucket_name_prefix() -> str:
    yield GENERAL_BUCKET_NAME_PREFIX + bucket_name_part(8)


@pytest.fixture(scope='module')
def monkey_patch():
    """ Module-scope monkeypatching (original `monkeypatch` is function-scope) """
    from _pytest.monkeypatch import MonkeyPatch
    monkey = MonkeyPatch()
    yield monkey
    monkey.undo()


@pytest.fixture(scope='module', autouse=True)
def auto_change_account_info_dir(monkey_patch) -> dir:
    """
    Automatically for the whole module testing:
    1) temporary remove B2_APPLICATION_KEY and B2_APPLICATION_KEY_ID from environment
    2) create a temporary directory for storing account info database
    """

    monkey_patch.delenv('B2_APPLICATION_KEY_ID', raising=False)
    monkey_patch.delenv('B2_APPLICATION_KEY', raising=False)

    # make b2sdk use temp dir for storing default & per-profile account information
    with TemporaryDirectory() as temp_dir:
        monkey_patch.setenv(B2_ACCOUNT_INFO_ENV_VAR, path.join(temp_dir, '.b2_account_info'))
        monkey_patch.setenv(XDG_CONFIG_HOME_ENV_VAR, temp_dir)
        yield temp_dir


@pytest.fixture(scope='module')
def b2_api(application_key_id, application_key, realm, this_run_bucket_name_prefix) -> Api:
    yield Api(
        application_key_id, application_key, realm, GENERAL_BUCKET_NAME_PREFIX,
        this_run_bucket_name_prefix
    )


@pytest.fixture(scope='module', autouse=True)
def auto_clean_buckets(b2_api, request, testrun_uid):
    """ Automatically clean buckets before and after the whole module testing """

    lock_file = Path(gettempdir()) / f'{testrun_uid}.lock'

    # lock file cannot be used to store info - use another file to track # of active workers
    worker_count_file = Path(gettempdir()) / f'{testrun_uid}.active-workers-count.txt'

    with FileLock(str(lock_file)):
        if not worker_count_file.is_file():
            b2_api.clean_buckets()
            worker_count_file.write_text('1')

        else:
            worker_count = int(worker_count_file.read_text())
            worker_count_file.write_text(str(worker_count + 1))

    yield

    if request.config.getoption('--cleanup'):
        with FileLock(str(lock_file)):
            worker_count = int(worker_count_file.read_text())
            worker_count_file.write_text(str(worker_count - 1))
            if worker_count == 1:
                b2_api.clean_buckets()
                worker_count_file.unlink()


@pytest.fixture(scope='module')
def b2_tool(
    request, application_key_id, application_key, realm, this_run_bucket_name_prefix
) -> CommandLine:
    tool = CommandLine(
        request.config.getoption('--sut'),
        application_key_id,
        application_key,
        realm,
        this_run_bucket_name_prefix,
    )
    tool.reauthorize(check_key_capabilities=True)  # reauthorize for the first time (with check)
    return tool


@pytest.fixture(scope='function', autouse=True)
def auto_reauthorize(request, b2_tool):
    """ Automatically reauthorize for each test (without check) """
    b2_tool.reauthorize(check_key_capabilities=False)
