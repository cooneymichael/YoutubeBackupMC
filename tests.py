"""Offline tests for backup_playlists.py.  No network, credentials or quota.

Run:  python -m unittest tests -v

The fake service (fake_youtube.py) is as strict as the real client about
argument names, so a typo or an extra keyword argument in the script shows up
here as a TypeError, and it can inject timeouts, connection resets, 503s and
quota errors to exercise the retry and error-handling paths.
"""
import contextlib
import io
import os
import tempfile
import unittest

import backup_playlists as bp
from fake_youtube import FakeService

PLAYLISTS_IN_FAKE = 7 + 1  # 7 regular + the one with a hidden item


def run_backup(fake):
    """Run the real bp.main() against `fake`; return (data, printed_output)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = (bp.get_service, bp.OUTPUT_FILE)
        bp.get_service = lambda: fake
        bp.OUTPUT_FILE = os.path.join(tmp, 'out.pkl')
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                bp.main()          # must not raise
            data = bp.load_backup(bp.OUTPUT_FILE)
        finally:
            bp.get_service, bp.OUTPUT_FILE = saved
    return data, out.getvalue()


class StrictFakeTests(unittest.TestCase):
    """Make sure the fake really would catch a bad call (otherwise the other
    tests prove nothing about TypeErrors)."""

    def test_unknown_keyword_is_typeerror(self):
        svc = FakeService(n_playlists=1)
        with self.assertRaises(TypeError):
            svc.playlistItems().list(part=['id'], playlistId='x', bogus=1)

    def test_missing_part_is_typeerror(self):
        svc = FakeService(n_playlists=1)
        with self.assertRaises(TypeError):
            svc.playlists().list(mine=True)

    def test_bad_execute_args_are_typeerror(self):
        svc = FakeService(n_playlists=1)
        req = svc.playlists().list(part=['id'], mine=True)
        with self.assertRaises(TypeError):
            req.execute(num_retries='3')

    def test_bad_part_and_missing_filter_are_http_errors(self):
        import googleapiclient.errors as errs
        svc = FakeService(n_playlists=1)
        with self.assertRaises(errs.HttpError):
            svc.playlists().list(part=['nonsense'], mine=True).execute()
        with self.assertRaises(errs.HttpError):
            svc.playlists().list(part=['id']).execute()      # no mine/id/channelId


class CleanRunTests(unittest.TestCase):
    def test_complete_and_correct(self):
        fake = FakeService()
        data, _ = run_backup(fake)
        self.assertTrue(data['complete'])
        self.assertEqual(data['errors'], [])
        self.assertEqual(len(data['playlists']), PLAYLISTS_IN_FAKE)
        for pl in data['playlists']:
            self.assertEqual(len(pl['videos']), len(fake._items[pl['id']]))
            ids = [v['contentDetails']['videoId'] for v in pl['videos']]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(all(v['url'].startswith('https://www.youtube.com/watch?v=')
                                for v in pl['videos']))
        self.assertEqual(set(data['special_playlists']), {'likes', 'uploads'})
        self.assertEqual(len(data['subscriptions']), len(fake._subs))
        self.assertEqual(fake.faults.injected, 0)

    def test_hidden_item_playlist_terminates_and_is_reported(self):
        fake = FakeService()
        data, printed = run_backup(fake)
        hidden = [p for p in data['playlists'] if 'hidden item' in p['snippet']['title']][0]
        self.assertEqual(len(hidden['videos']), 317)
        self.assertEqual(hidden['contentDetails']['itemCount'], 318)
        self.assertIn('1 hidden by the API', printed)


class TransientFailureTests(unittest.TestCase):
    """Failures that go away within NUM_RETRIES attempts must not lose data."""

    def assert_full_backup(self, fake, data):
        self.assertTrue(data['complete'])
        self.assertEqual(data['errors'], [])
        self.assertEqual(len(data['playlists']), PLAYLISTS_IN_FAKE)
        for pl in data['playlists']:
            self.assertEqual(len(pl['videos']), len(fake._items[pl['id']]))
        self.assertEqual(len(data['subscriptions']), len(fake._subs))

    def test_timeouts_are_retried(self):
        fake = FakeService(faults=[{'resource': 'playlistItems', 'kind': 'timeout', 'times': 3}])
        data, _ = run_backup(fake)
        self.assertEqual(fake.faults.injected, 3)
        self.assertEqual(fake.faults.retries, 3)
        self.assert_full_backup(fake, data)

    def test_connection_reset_is_retried(self):
        fake = FakeService(faults=[{'resource': 'playlists', 'kind': 'reset', 'times': 2}])
        data, _ = run_backup(fake)
        self.assertEqual(fake.faults.retries, 2)
        self.assert_full_backup(fake, data)

    def test_503_is_retried_on_every_resource(self):
        rules = [{'resource': r, 'kind': '503', 'times': 2}
                 for r in ('playlists', 'playlistItems', 'channels', 'subscriptions')]
        fake = FakeService(faults=rules)
        data, _ = run_backup(fake)
        self.assertEqual(fake.faults.retries, 8)
        self.assert_full_backup(fake, data)

    def test_max_retries_exactly_still_succeeds(self):
        # NUM_RETRIES retries = NUM_RETRIES + 1 attempts; failing all but the last succeeds
        fake = FakeService(faults=[{'resource': 'subscriptions', 'kind': 'timeout',
                                    'times': bp.NUM_RETRIES}])
        data, _ = run_backup(fake)
        self.assert_full_backup(fake, data)


class PersistentFailureTests(unittest.TestCase):
    """Failures that outlast the retries must be recorded, not crash the run."""

    def test_one_playlist_keeps_timing_out(self):
        fake = FakeService()
        victim = fake._playlists[3]
        fake.faults.rules.append({'resource': 'playlistItems', 'kind': 'timeout',
                                  'times': None, 'playlist_id': victim['id'], '_used': 0})
        data, _ = run_backup(fake)
        self.assertTrue(data['complete'])
        self.assertEqual([e['playlist_id'] for e in data['errors']], [victim['id']])
        saved_ids = {p['id'] for p in data['playlists']}
        self.assertNotIn(victim['id'], saved_ids)          # not saved as an empty playlist
        self.assertEqual(len(saved_ids), PLAYLISTS_IN_FAKE - 1)
        self.assertEqual(len(data['subscriptions']), len(fake._subs))

    def test_retries_exhausted_then_recovers(self):
        # fails NUM_RETRIES+1 times: the first request gives up, later ones work
        fake = FakeService(faults=[{'resource': 'playlistItems', 'kind': 'timeout',
                                    'times': bp.NUM_RETRIES + 1}])
        data, _ = run_backup(fake)
        self.assertEqual(len(data['errors']), 1)
        self.assertEqual(len(data['playlists']), PLAYLISTS_IN_FAKE - 1)
        self.assertTrue(data['complete'])

    def test_quota_exceeded_is_not_retried(self):
        fake = FakeService(faults=[{'resource': 'playlistItems', 'kind': 'quota', 'times': None}])
        data, _ = run_backup(fake)
        self.assertEqual(fake.faults.retries, 0)           # 403 quota is not a transient error
        self.assertEqual(len(data['playlists']), 0)
        self.assertTrue(len(data['errors']) >= PLAYLISTS_IN_FAKE)
        self.assertEqual(len(data['subscriptions']), len(fake._subs))

    def test_subscriptions_failure_is_recorded(self):
        fake = FakeService(faults=[{'resource': 'subscriptions', 'kind': 'timeout', 'times': None}])
        data, _ = run_backup(fake)
        self.assertEqual(data['subscriptions'], [])
        self.assertEqual([e['title'] for e in data['errors']], ['subscriptions'])
        self.assertEqual(len(data['playlists']), PLAYLISTS_IN_FAKE)
        self.assertTrue(data['complete'])

    def test_special_playlist_lookup_failure_is_recorded(self):
        fake = FakeService(faults=[{'resource': 'channels', 'kind': 'timeout', 'times': None}])
        data, _ = run_backup(fake)
        self.assertEqual(data['special_playlists'], {})
        self.assertEqual(len(data['playlists']), PLAYLISTS_IN_FAKE)
        self.assertEqual(len(data['subscriptions']), len(fake._subs))
        self.assertTrue(data['complete'])

    def test_initial_playlist_listing_failure_is_recorded(self):
        fake = FakeService(faults=[{'resource': 'playlists', 'kind': 'quota', 'times': None}])
        data, _ = run_backup(fake)
        self.assertEqual(data['playlists'], [])
        self.assertTrue(data['errors'])
        self.assertEqual(len(data['subscriptions']), len(fake._subs))
        self.assertTrue(data['complete'])


class FileFormatTests(unittest.TestCase):
    def test_truncated_file_keeps_earlier_records(self):
        fake = FakeService()
        with tempfile.TemporaryDirectory() as tmp:
            saved = (bp.get_service, bp.OUTPUT_FILE)
            bp.get_service = lambda: fake
            bp.OUTPUT_FILE = os.path.join(tmp, 'out.pkl')
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    bp.main()
                size = os.path.getsize(bp.OUTPUT_FILE)
                with open(bp.OUTPUT_FILE, 'r+b') as f:
                    f.truncate(size - 3000)
                with contextlib.redirect_stdout(io.StringIO()):
                    data = bp.load_backup(bp.OUTPUT_FILE)
            finally:
                bp.get_service, bp.OUTPUT_FILE = saved
        self.assertFalse(data['complete'])
        self.assertGreater(len(data['playlists']), 0)


if __name__ == '__main__':
    unittest.main()
