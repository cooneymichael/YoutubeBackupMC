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

import socket

import httplib2
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


def _http_error(status, reason):
    resp = httplib2.Response({'status': status})
    body = ('{"error": {"code": %d, "message": "%s", "errors": [{"reason": "%s"}]}}'
            % (status, reason, reason)).encode()
    return googleapiclient.errors.HttpError(resp, body)


class FaultPlan:
    """Injects failures into requests.

    Each rule is a dict:
        resource     'playlists' | 'playlistItems' | 'channels' | 'subscriptions'
        kind         'timeout' | 'reset' | '503' | 'quota'
        times        how many matching attempts fail (None = every attempt)
        playlist_id  optional; only requests for that playlist match
    """
    RETRIABLE_STATUS = (429, 500, 502, 503, 504)

    def __init__(self, rules=None):
        self.rules = [dict(r, _used=0) for r in (rules or [])]
        self.retries = 0       # attempts that failed and were retried
        self.injected = 0      # total failures injected

    def maybe_raise(self, resource, params):
        for r in self.rules:
            if r['resource'] != resource:
                continue
            if r.get('playlist_id') and r['playlist_id'] != params.get('playlistId'):
                continue
            if r.get('times') is not None and r['_used'] >= r['times']:
                continue
            r['_used'] += 1
            self.injected += 1
            kind = r['kind']
            if kind == 'timeout':
                raise socket.timeout('timed out')
            if kind == 'reset':
                raise ConnectionResetError(104, 'Connection reset by peer')
            if kind == '503':
                raise _http_error(503, 'backendError')
            if kind == 'quota':
                raise _http_error(403, 'quotaExceeded')
            raise ValueError(f'unknown fault kind {kind!r}')

    @classmethod
    def is_retriable(cls, exc):
        """Mirrors what googleapiclient retries: timeouts, resets, 429/5xx."""
        if isinstance(exc, (socket.timeout, ConnectionError)):
            return True
        return (isinstance(exc, googleapiclient.errors.HttpError)
                and exc.resp.status in cls.RETRIABLE_STATUS)


class _FakeRequest:
    """Like googleapiclient's HttpRequest: execute(http=None, num_retries=0)."""

    def __init__(self, fn, faults, resource, params):
        self._fn, self._faults = fn, faults
        self._resource, self._params = resource, params

    def execute(self, http=None, num_retries=0):
        if not isinstance(num_retries, int) or num_retries < 0:
            raise TypeError(f'num_retries must be a non-negative int, got {num_retries!r}')
        last = None
        for _ in range(num_retries + 1):
            try:
                self._faults.maybe_raise(self._resource, self._params)
                return self._fn()
            except Exception as e:                  # noqa: BLE001
                if not FaultPlan.is_retriable(e):
                    raise                           # e.g. 403 quota, 404, 400
                self._faults.retries += 1
                last = e                            # (real client sleeps with backoff here)
        raise last


class _Resource:
    """One API collection, e.g. service.playlists().

    list() is as strict as the real generated client methods:
      * an unknown keyword argument raises TypeError immediately
      * a missing required 'part' raises TypeError immediately
      * None values are dropped (the real client does this too)
      * bad 'part' names, maxResults outside 0-50, or no filter parameter
        become a 400 HttpError when execute() is called
    """
    name = None
    valid_params = set()
    valid_parts = set()
    filters = set()         # at least one of these must be supplied

    def __init__(self, rows_for, faults):
        self._rows_for = rows_for
        self._faults = faults
        self.calls = 0  # handy for asserting how many API calls were made

    def list(self, **kwargs):
        unknown = set(kwargs) - self.valid_params
        if unknown:
            raise TypeError(f'Got an unexpected keyword argument {sorted(unknown)[0]}')
        params = {k: v for k, v in kwargs.items() if v is not None}
        if 'part' not in params:
            raise TypeError("Missing required parameter 'part'")
        self.calls += 1

        def run():
            parts = params['part'].split(',') if isinstance(params['part'], str) else list(params['part'])
            bad = [p for p in parts if p not in self.valid_parts]
            if bad:
                raise _http_error(400, f'unknownPart: {bad}')
            max_results = params.get('maxResults', 5)
            if not isinstance(max_results, int) or not 0 <= max_results <= 50:
                raise _http_error(400, 'invalidMaxResults')
            if not (self.filters & set(params)):
                raise _http_error(400, f'missingFilter: one of {sorted(self.filters)}')
            token = params.get('pageToken')
            if token is not None and not isinstance(token, str):
                raise _http_error(400, 'invalidPageToken')
            rows, ghost = self._rows(params)
            return self._page(rows, max_results, token, ghost)

        return _FakeRequest(run, self._faults, self.name, params)

    def _rows(self, params):
        return self._rows_for(), False

    def _page(self, rows, maxResults=5, pageToken=None, ghost=False):
        """ghost=True reproduces a real-API quirk: the playlist contains an
        item that is hidden from results (private/deleted), so totalResults is
        one higher than the items you can ever fetch, and nextPageToken keeps
        being returned on empty pages forever."""
        start = int(pageToken) if pageToken else 0
        end = start + maxResults
        resp = {
            'kind': 'youtube#listResponse',
            'etag': _rand_id(n=27),
            'pageInfo': {'totalResults': len(rows) + (1 if ghost else 0),
                         'resultsPerPage': maxResults},
            'items': rows[start:end],
        }
        if ghost or end < len(rows):
            resp['nextPageToken'] = str(end)  # opaque token, like the real API
        return resp


class _Playlists(_Resource):
    name = 'playlists'
    valid_params = {'part', 'channelId', 'id', 'mine', 'hl', 'maxResults', 'pageToken',
                    'onBehalfOfContentOwner', 'onBehalfOfContentOwnerChannel'}
    valid_parts = {'contentDetails', 'id', 'localizations', 'player', 'snippet', 'status'}
    filters = {'channelId', 'id', 'mine'}


class _PlaylistItems(_Resource):
    name = 'playlistItems'
    valid_params = {'part', 'id', 'maxResults', 'onBehalfOfContentOwner',
                    'pageToken', 'playlistId', 'videoId'}
    valid_parts = {'contentDetails', 'id', 'snippet', 'status'}
    filters = {'playlistId', 'id'}
    ghost_ids = set()

    def _rows(self, params):
        pid = params.get('playlistId')
        rows = self._rows_for(pid)
        if rows is None:
            raise _http_error(404, 'playlistNotFound')   # not retriable
        return rows, pid in self.ghost_ids


class _Channels(_Resource):
    name = 'channels'
    valid_params = {'part', 'categoryId', 'forHandle', 'forUsername', 'hl', 'id',
                    'managedByMe', 'maxResults', 'mine', 'mySubscribers',
                    'onBehalfOfContentOwner', 'pageToken'}
    valid_parts = {'auditDetails', 'brandingSettings', 'contentDetails',
                   'contentOwnerDetails', 'id', 'localizations', 'snippet',
                   'statistics', 'status', 'topicDetails'}
    filters = {'categoryId', 'forHandle', 'forUsername', 'id', 'managedByMe',
               'mine', 'mySubscribers'}


class _Subscriptions(_Resource):
    name = 'subscriptions'
    valid_params = {'part', 'channelId', 'forChannelId', 'id', 'maxResults', 'mine',
                    'myRecentSubscribers', 'mySubscribers', 'onBehalfOfContentOwner',
                    'onBehalfOfContentOwnerChannel', 'order', 'pageToken'}
    valid_parts = {'contentDetails', 'id', 'snippet', 'subscriberSnippet'}
    filters = {'channelId', 'id', 'mine', 'myRecentSubscribers', 'mySubscribers'}


class FakeService:
    def __init__(self, n_playlists=7, seed=42, max_videos=120, faults=None):
        rng = random.Random(seed)
        random.seed(seed)
        self.faults = FaultPlan(faults)

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

        # a playlist with one hidden item, like the "recipes/cooking" one that
        # made the real script loop forever (317 fetchable, 318 reported)
        gid = _rand_id('PL', 32)
        self._items[gid] = [self._make_item(rng, gid, pos, 'Ghost') for pos in range(317)]
        ghost = dict(self._playlists[0])
        ghost.update(id=gid, snippet=dict(ghost['snippet'], title='Fake Playlist (hidden item)'),
                     contentDetails={'itemCount': 318})
        self._playlists.append(ghost)
        self._ghost_ids = {gid}

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

        self._subs = []
        for n in range(rng.randint(60, 130)):
            cid = _rand_id('UC', 22)
            self._subs.append({
                'kind': 'youtube#subscription',
                'etag': _rand_id(n=27),
                'id': _rand_id(n=48),
                'snippet': {
                    'publishedAt': _timestamp(rng),
                    'title': f'Fake Channel {n}',
                    'description': f'Description of fake channel {n}.',
                    'resourceId': {'kind': 'youtube#channel', 'channelId': cid},
                    'channelId': 'UCfakechannel0000000000',
                    'thumbnails': _thumbs(cid),
                },
                'contentDetails': {'totalItemCount': rng.randint(1, 900),
                                   'newItemCount': rng.randint(0, 20),
                                   'activityType': 'all'},
            })

        self._playlists_res = _Playlists(lambda: self._playlists, self.faults)
        self._items_res = _PlaylistItems(lambda pid: self._items.get(pid), self.faults)
        self._items_res.ghost_ids = self._ghost_ids
        self._channels_res = _Channels(lambda: self._channel, self.faults)
        self._subs_res = _Subscriptions(lambda: self._subs, self.faults)

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

    def subscriptions(self):
        return self._subs_res


if __name__ == '__main__':
    import backup_playlists

    fake = FakeService()
    backup_playlists.get_service = lambda: fake   # skip OAuth entirely
    backup_playlists.OUTPUT_FILE = 'fake_youtube_playlists.pkl'
    backup_playlists.main()

    # sanity check: the pickle should hold every fake video
    data = backup_playlists.load_backup(backup_playlists.OUTPUT_FILE)
    assert data['complete'], 'run did not finish'
    for pl in data['playlists']:
        assert len(pl['videos']) == len(fake._items[pl['id']]), pl['snippet']['title']
        assert len({v['contentDetails']['videoId'] for v in pl['videos']}) == len(pl['videos'])
    for name, entry in data['special_playlists'].items():
        assert len(entry['videos']) == len(fake._items[entry['id']]), name
    assert len(data['subscriptions']) == len(fake._subs), 'subscriptions'
    assert all(s['url'].startswith('https://www.youtube.com/channel/UC')
               for s in data['subscriptions'])
    print('\nAll counts match. API calls made:',
          f'playlists={fake.playlists().calls}',
          f'playlistItems={fake.playlistItems().calls}',
          f'channels={fake.channels().calls}',
          f'subscriptions={fake.subscriptions().calls}')
