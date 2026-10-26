"""Offline stand-in for the YouTube Data API, for testing backup_playlists.py.

FakeService mimics the pieces of the googleapiclient service object that the
backup script touches:

    service.playlists().list(...).execute()
    service.playlistItems().list(...).execute()
    service.channels().list(...).execute()

Responses are shaped like the real API (kind/etag/pageInfo/nextPageToken/items)
and are paginated, so the script's paging loop is genuinely exercised.  No
network access, credentials, or quota are used.

Run this file directly to execute the real backup_playlists.main() against the
fake data; it writes the pickle just as the real run would.
"""
import random
import string
import datetime

import googleapiclient.errors


def _rand_id(prefix='', n=11):
    chars = string.ascii_letters + string.digits + '-_'
    return prefix + ''.join(random.choices(chars, k=n))


def _timestamp(rng, days_back=2000):
    delta = datetime.timedelta(days=rng.randint(0, days_back),
                               seconds=rng.randint(0, 86399))
    t = datetime.datetime.now(datetime.timezone.utc) - delta
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def _thumbs(video_id):
    base = f'https://i.ytimg.com/vi/{video_id}'
    return {
        'default': {'url': f'{base}/default.jpg', 'width': 120, 'height': 90},
        'medium': {'url': f'{base}/mqdefault.jpg', 'width': 320, 'height': 180},
        'high': {'url': f'{base}/hqdefault.jpg', 'width': 480, 'height': 360},
    }


class _FakeRequest:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Resource:
    """Shared paging logic: slices self._rows by maxResults/pageToken."""

    def __init__(self, rows_for):
        self._rows_for = rows_for
        self.calls = 0  # handy for asserting how many API calls were made

    def _page(self, rows, maxResults=5, pageToken=None):
        start = int(pageToken) if pageToken else 0
        end = start + maxResults
        resp = {
            'kind': 'youtube#listResponse',
            'etag': _rand_id(n=27),
            'pageInfo': {'totalResults': len(rows), 'resultsPerPage': maxResults},
            'items': rows[start:end],
        }
        if end < len(rows):
            resp['nextPageToken'] = str(end)  # opaque token, like the real API
        return resp


class _Playlists(_Resource):
    def list(self, part=None, mine=None, maxResults=5, pageToken=None, **_):
        self.calls += 1
        return _FakeRequest(lambda: self._page(self._rows_for(), maxResults, pageToken))


class _PlaylistItems(_Resource):
    def list(self, part=None, playlistId=None, maxResults=5, pageToken=None, **_):
        self.calls += 1

        def run():
            rows = self._rows_for(playlistId)
            if rows is None:
                # real API returns 404 playlistNotFound; mimic with HttpError
                import httplib2
                resp = httplib2.Response({'status': 404})
                raise googleapiclient.errors.HttpError(
                    resp, b'{"error": {"message": "playlistNotFound"}}')
            return self._page(rows, maxResults, pageToken)
        return _FakeRequest(run)


class _Channels(_Resource):
    def list(self, part=None, mine=None, maxResults=5, pageToken=None, **_):
        self.calls += 1
        return _FakeRequest(lambda: self._page(self._rows_for(), maxResults, pageToken))


class FakeService:
    def __init__(self, n_playlists=7, seed=42, max_videos=120):
        rng = random.Random(seed)
        random.seed(seed)

        self._items = {}      # playlist_id -> [playlistItem, ...]
        self._playlists = []

        for n in range(n_playlists):
            pid = _rand_id('PL', 32)
            # Sizes chosen to hit edge cases: empty, exactly one page (50),
            # just over a page boundary (51), and a few multi-page ones.
            size = {0: 0, 1: 50, 2: 51}.get(n, rng.randint(1, max_videos))
            self._items[pid] = [self._make_item(rng, pid, pos, f'Playlist {n}')
                                for pos in range(size)]
            self._playlists.append({
                'kind': 'youtube#playlist',
                'etag': _rand_id(n=27),
                'id': pid,
                'snippet': {
                    'publishedAt': _timestamp(rng),
                    'channelId': 'UCfakechannel0000000000',
                    'title': f'Fake Playlist {n}',
                    'description': f'Auto-generated test playlist #{n}',
                    'thumbnails': _thumbs(_rand_id()),
                    'channelTitle': 'Test Channel',
                    'localized': {'title': f'Fake Playlist {n}',
                                  'description': f'Auto-generated test playlist #{n}'},
                },
                'status': {'privacyStatus': rng.choice(['public', 'private', 'unlisted'])},
                'contentDetails': {'itemCount': size},
                'player': {'embedHtml': '<iframe width="640" height="360" ...></iframe>'},
            })

        # special playlists reachable only through channels().list
        self._related = {
            'likes': _rand_id('LL', 22),
            'uploads': _rand_id('UU', 22),
            'watchHistory': 'HL',
            'watchLater': 'WL',
        }
        self._items[self._related['likes']] = [
            self._make_item(rng, self._related['likes'], p, 'Liked') for p in range(rng.randint(60, 130))]
        self._items[self._related['uploads']] = [
            self._make_item(rng, self._related['uploads'], p, 'Upload') for p in range(rng.randint(0, 8))]
        # Watch Later / History: the real API refuses these; leave them absent so
        # they would 404 if anything asked for them.

        self._channel = [{
            'kind': 'youtube#channel',
            'id': 'UCfakechannel0000000000',
            'contentDetails': {'relatedPlaylists': self._related},
        }]

        self._playlists_res = _Playlists(lambda: self._playlists)
        self._items_res = _PlaylistItems(lambda pid: self._items.get(pid))
        self._channels_res = _Channels(lambda: self._channel)

    @staticmethod
    def _make_item(rng, playlist_id, position, label):
        video_id = _rand_id()
        deleted = rng.random() < 0.04
        private = (not deleted) and rng.random() < 0.03
        title = ('Deleted video' if deleted else
                 'Private video' if private else
                 f'{label} video #{position}: {rng.choice(["Tutorial", "Vlog", "Music", "Review", "Talk"])}')
        snippet = {
            'publishedAt': _timestamp(rng),
            'channelId': 'UCfakechannel0000000000',
            'title': title,
            'description': ('This video is unavailable.' if deleted or private
                            else 'Lorem ipsum dolor sit amet, consectetur adipiscing elit.'),
            'playlistId': playlist_id,
            'position': position,
            'resourceId': {'kind': 'youtube#video', 'videoId': video_id},
        }
        if not (deleted or private):
            snippet['thumbnails'] = _thumbs(video_id)
            snippet['channelTitle'] = 'Test Channel'
            snippet['videoOwnerChannelTitle'] = rng.choice(['Alice TV', 'Bob Codes', 'Carol Music'])
            snippet['videoOwnerChannelId'] = _rand_id('UC', 22)
        return {
            'kind': 'youtube#playlistItem',
            'etag': _rand_id(n=27),
            'id': _rand_id(n=48),
            'snippet': snippet,
            'contentDetails': {'videoId': video_id,
                               'videoPublishedAt': _timestamp(rng)},
            'status': {'privacyStatus': 'private' if private else 'public'},
        }

    # --- the three resources the backup script uses ---
    def playlists(self):
        return self._playlists_res

    def playlistItems(self):
        return self._items_res

    def channels(self):
        return self._channels_res


if __name__ == '__main__':
    import pickle
    import backup_playlists

    fake = FakeService()
    backup_playlists.get_service = lambda: fake   # skip OAuth entirely
    backup_playlists.OUTPUT_FILE = 'fake_youtube_playlists.pkl'
    backup_playlists.main()

    # sanity check: the pickle should hold every fake video
    with open(backup_playlists.OUTPUT_FILE, 'rb') as f:
        data = pickle.load(f)
    for pl in data['playlists']:
        assert len(pl['videos']) == len(fake._items[pl['id']]), pl['snippet']['title']
        assert len({v['contentDetails']['videoId'] for v in pl['videos']}) == len(pl['videos'])
    for name, entry in data['special_playlists'].items():
        assert len(entry['videos']) == len(fake._items[entry['id']]), name
    print('\nAll counts match. API calls made:',
          f'playlists={fake.playlists().calls}',
          f'playlistItems={fake.playlistItems().calls}',
          f'channels={fake.channels().calls}')
