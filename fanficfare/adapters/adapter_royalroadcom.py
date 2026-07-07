# -*- coding: utf-8 -*-

# Copyright 2011 Fanficdownloader team, 2018 FanFicFare team
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
#

from __future__ import absolute_import
import contextlib
from datetime import datetime
import json
import logging
import re
from .. import exceptions as exceptions
from ..dateutils import parse_relative_date_string
from ..htmlcleanup import stripHTML

from .base_adapter import BaseSiteAdapter

logger = logging.getLogger(__name__)


def getClass():
    return RoyalRoadAdapter

# Class name has to be unique.  Our convention is camel case the
# sitename with Adapter at the end.  www is skipped.
class RoyalRoadAdapter(BaseSiteAdapter):

    def __init__(self, config, url):
        BaseSiteAdapter.__init__(self, config, url)

        self.username = "NoneGiven" # if left empty, site doesn't return any message at all.
        self.password = ""
        self.is_adult=False

        # get storyId from url--url validation guarantees query is only fiction/1234
        self.story.setMetadata('storyId',re.match(r'/fiction/(\d+)(/.*)?$',self.parsedUrl.path).groups()[0])


        # normalized story URL.
        self._setURL('https://' + self.getSiteDomain() + '/fiction/'+self.story.getMetadata('storyId'))

        # Each adapter needs to have a unique site abbreviation.
        self.story.setMetadata('siteabbrev','rylrdl')

        # The date format will vary from site to site.
        # http://docs.python.org/library/datetime.html#strftime-strptime-behavior
        self.dateformat = '%d/%m/%Y %H:%M:%S %p'

        # RR has globally unique ID for each chapter which can be used for fast lookup
        self.chapterURLIndex = {}

        # Maps chapter_id -> (wayback timestamp, archived original URL) for
        # chapters that need to be fetched from the Wayback Machine. The
        # archived URL is preserved separately because the slug stored in
        # chapterUrls (taken from an archived ToC) may differ from the slug
        # under which the chapter itself was archived.
        self.wayback_chapters = {}

    def _make_image_fetch(self, wayback_ts=None):
        """Create a fetch function that:
        1. Falls back to Wayback Machine on image failure
        2. Extracts first frame from large animated GIF/WebP (over 1MB)
        3. Compresses other large images (over 1MB)"""
        original_fetch = self.get_request_raw
        adapter = self
        def image_fetch_wrapper(url, **kwargs):
            try:
                data = original_fetch(url, **kwargs)
            except Exception:
                if kwargs.get('image', False):
                    logger.info("Image fetch failed for %s, trying Wayback Machine"
                                % url)
                    data = adapter._wayback_fetch_image(url, wayback_ts)
                else:
                    raise
            if kwargs.get('image', False) and len(data) > 1000000:
                data = adapter._flatten_large_image(data, url)
            return data
        return image_fetch_wrapper

    def _flatten_large_image(self, data, url):
        """For images over 1MB: extract first frame from animated GIF/WebP,
        or compress static images to JPEG."""
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(data))

            # Animated GIF/WebP: extract first frame
            if getattr(img, 'is_animated', False):
                img.seek(0)
                if img.mode not in ('RGB', 'L'):
                    img = img.convert('RGBA').convert('RGB')
                out = BytesIO()
                img.save(out, 'JPEG', quality=85, optimize=True)
                result = out.getvalue()
                logger.info("Extracted first frame from %dKB animated %s "
                            "-> %dKB JPEG: %s"
                            % (len(data) // 1024,
                               img.format or 'image',
                               len(result) // 1024, url))
                return result

            # Static image over 1MB: compress to JPEG
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGBA').convert('RGB')
            out = BytesIO()
            img.save(out, 'JPEG', quality=75, optimize=True)
            result = out.getvalue()
            if len(result) < len(data):
                logger.info("Compressed %dKB %s -> %dKB JPEG: %s"
                            % (len(data) // 1024,
                               img.format or 'image',
                               len(result) // 1024, url))
                return result
        except Exception as e:
            logger.debug("Image compression failed for %s: %s" % (url, e))
        return data

    def _is_wayback_redirect_capture(self, data):
        """Check if Wayback response is a redirect capture page instead of
        actual content (Wayback stores these when the original server
        returned a 3xx redirect at crawl time)."""
        if not data:
            return True
        check = data[:1000] if isinstance(data, str) else \
            data[:1000].decode('utf-8', errors='ignore')
        check = check.lower()
        return 'got an http 30' in check and 'redirecting to' in check

    def _wayback_fetch_image(self, url, preferred_ts=None):
        """Fetch image from Wayback Machine, trying snapshots newest-first
        and skipping any that are redirect captures."""
        # Build ordered list of timestamps to try (preferred first)
        timestamps = []
        if preferred_ts:
            timestamps.append(preferred_ts)

        # Query CDX for available snapshots with actual content (status 200)
        try:
            cdx_url = ('https://web.archive.org/cdx/search/cdx'
                       '?url=%s'
                       '&output=json'
                       '&fl=timestamp,statuscode'
                       '&filter=statuscode:200') % url
            resp = self.get_request(cdx_url)
            rows = json.loads(resp)
            if rows and isinstance(rows[0], list) and rows[0][0] == 'timestamp':
                rows = rows[1:]
            # Add timestamps newest-first, skip any already queued
            seen = set(timestamps)
            for row in reversed(rows):
                ts = row[0]
                if ts not in seen:
                    timestamps.append(ts)
                    seen.add(ts)
        except Exception as e:
            logger.debug("CDX query for image %s failed: %s" % (url, e))

        # Try up to 5 snapshots
        for ts in timestamps[:5]:
            try:
                wayback_url = 'https://web.archive.org/web/%sid_/%s' % (ts, url)
                data = self.get_request_raw(wayback_url, image=True)
                if not self._is_wayback_redirect_capture(data):
                    logger.info("Found image in Wayback at timestamp %s: %s"
                                % (ts, url))
                    return data
                logger.debug("Wayback snapshot %s for %s was a redirect capture,"
                             " trying next" % (ts, url))
            except Exception:
                continue

        raise Exception("No working Wayback snapshot found for image: %s" % url)

    def make_date(self, parenttag):
        # locale dates differ but the timestamp is easily converted
        timetag = parenttag.find('time')
        if timetag.has_attr('unixtime'):
            ts = timetag['unixtime']
            return datetime.fromtimestamp(float(ts))
        else:
            ## site has gone to crappy resolution "XX
            ## (min/day/month/year/etc) ago" dating
            return parse_relative_date_string(timetag.text)

    @staticmethod # must be @staticmethod, don't remove it.
    def getSiteDomain():
        # The site domain.  Does have www here, if it uses it.
        # changed from royalroadl.com
        return 'www.royalroad.com'

    @classmethod
    def getAcceptDomains(cls):
        return ['royalroad.com','royalroadl.com','www.royalroadl.com']

    @classmethod
    def getConfigSections(cls):
        "Only needs to be overriden if has additional ini sections."
        return ['royalroadl.com',cls.getSiteDomain()]

    @classmethod
    def getSiteExampleURLs(cls):
        return "https://www.royalroad.com/fiction/3056"

    @classmethod
    def get_section_url(cls,url):
        ## minimal URL used for section names in INI and reject list
        ## for comparison
        # logger.debug("pre--url:%s"%url)
        # https://www.royalroad.com/fiction/36051/memories-of-the-fall
        # https://www.royalroad.com/fiction/36051
        url = re.sub(r'^https?://(.*/fiction/\d+).*$',r'https://\1',url)
        # logger.debug("post-url:%s"%url)
        return url

    def getSiteURLPattern(self):
        return "https?"+re.escape("://")+r"(www\.|)royalroadl?\.com/fiction/\d+(/.*)?$"


    # rr won't send you future updates if you aren't 'caught up'
    # on the story.  Login isn't required but logging in will
    # mark stories you've downloaded as 'read' on rr.
    def performLogin(self):
        params = {}

        if self.password:
            params['Email'] = self.username
            params['password'] = self.password
        else:
            params['Email'] = self.getConfig("username")
            params['password'] = self.getConfig("password")

        if not params['password']:
            return

        loginUrl = 'https://' + self.getSiteDomain() + '/account/login'
        logger.debug("Will now login to URL (%s) as (%s)" % (loginUrl,
                                                              params['Email']))

        ## need to pull empty login page first to get request token
        soup = self.make_soup(self.get_request(loginUrl))
        ## FYI, this will fail if cookiejar is shared, but
        ## use_basic_cache is false.
        params['__RequestVerificationToken']=soup.find('input', {'name':'__RequestVerificationToken'})['value']

        d = self.post_request(loginUrl, params)
        if "Sign in" in d : #Member Account
            logger.info("Failed to login to URL %s as %s (requires Email not name)" % (loginUrl,
                                                             params['Email']))
            raise exceptions.FailedToLogin(self.url,"Failed to login as %s (RoyalRoad requires Email not name)" % params['Email'])
            return False
        else:
            return True

    ## RR chapter URL only requires the chapter ID number field to be correct, story ID and title values are ignored
    ## URL format after the domain /fiction/ is long form, storyID/storyTitle/chapter/chapterID/chapterTitle
    ##  short form has /fiction/chapter/chapterID    both forms have optional final /
    ## The regex matches both, and is valid if either there are both storyID/storyTitle and chapterTitle fields
    ##    or if there are neither of those two fields
    ## In addition, the chapterID must be found in chapterURLIndex table that is built when the ToC metadata is read.
    def normalize_chapterurl(self,url):
        chap_pattern = r"https?://(?:www\.)?royalroadl?\.com/fiction(/\d+/[^/]+)?/chapter/(\d+)(/[^/]+)?/?$"
        match = re.match(chap_pattern, url)
        if match and ((match.group(1) and match.group(3)) or (not match.group(1) and not match.group(3))):
            chapter_url_index = self.chapterURLIndex.get(match.group(2))
            if chapter_url_index is not None:
                return self.chapterUrls[chapter_url_index]['url']
        return url


		
    def make_soup(self, data):
        soup = super(RoyalRoadAdapter, self).make_soup(data)
    # Parse and store styles in a set
        self.styles_to_ignore = set()
        style_elements = soup.find_all('style')
        for style_element in style_elements:
            class_matches = re.findall(r'\.(\S+)\s*\{[^\}]*display\s*:\s*none\s*;[^\}]*\}', style_element.string, flags=re.IGNORECASE)
            if class_matches:
                self.styles_to_ignore.update(class_matches)
                del class_matches
        self.handle_spoilers(soup)
        return soup

    def handle_spoilers(self,topsoup):
        '''
        Modifies tag given as required to do spoiler changes.
        '''
        if self.getConfig('remove_spoilers'):
            for div in topsoup.find_all('div',class_='spoiler'):
                div.extract()
        elif self.getConfig('legend_spoilers'):
            for div in topsoup.find_all('div',class_='spoiler'):
                div.name='fieldset'
                legend = topsoup.new_tag('legend')
                smalltext = div.find('div',class_='smalltext')
                if smalltext:
                    legend.string = stripHTML(smalltext)
                    smalltext.extract()
                div.insert(0,legend)
                for inner in div.find_all('div',class_='spoiler-inner'):
                    del inner['style']
                #div.button.extract()

    ## Wayback Machine helpers for recovering stubbed chapters

    @staticmethod
    def _ensure_https(url):
        """Ensure URL has https:// scheme."""
        if not url.startswith('http'):
            return 'https://' + url
        return url

    def _wayback_cdx_query(self, url_pattern, extra_filter=None, collapse=True):
        """Query Wayback CDX API for archived snapshots matching url_pattern.
        Returns list of [original_url, timestamp, statuscode] entries.
        extra_filter: additional CDX filter expression, e.g. a positive
        regex on the original URL. collapse=False returns every capture
        instead of one per unique URL."""
        cdx_url = (
            'https://web.archive.org/cdx/search/cdx'
            '?url=%s'
            '&output=json'
            '&fl=original,timestamp,statuscode'
            '&filter=statuscode:200'
            '&filter=!original:.*[%%3F].*'
        ) % url_pattern
        if extra_filter:
            cdx_url += '&filter=' + extra_filter
        if collapse:
            cdx_url += '&collapse=urlkey'
        try:
            data = self.get_request(cdx_url, usecache=False)
            rows = json.loads(data)
            if rows and rows[0] == ['original', 'timestamp', 'statuscode']:
                return rows[1:]
            return rows
        except Exception as e:
            logger.warning("Wayback CDX query failed for %s: %s" % (url_pattern, e))
            return []

    def _wayback_probe_chapter(self, url, timestamp):
        """Fetch an archived chapter page and return (has_content, title).
        has_content is False for redirect captures and for pages without a
        chapter content div — e.g. RR's "Forbidden" page for a chapter the
        author deleted, which RR serves with HTTP 200 so Wayback archives
        it as a 200 capture."""
        try:
            data = self._wayback_fetch_raw(url, timestamp)
            if self._is_wayback_redirect_capture(data):
                return False, None
            soup = self.make_soup(data)
            div = soup.find('div', {'class': 'chapter-inner chapter-content'})
            title = None
            h1 = soup.find('h1')
            if h1:
                title = h1.get_text(strip=True) or None
            return (div is not None), title
        except Exception as e:
            logger.debug("Failed to probe archived chapter %s: %s" % (url, e))
            return False, None

    def _wayback_resolve_missing_chapter(self, chap_id, chapter_snapshots):
        """Find an archived capture of chap_id that actually contains
        chapter content, trying the newest capture first then older ones.
        Returns (timestamp, url, title), or None if no capture has content
        (typically a chapter deleted from the site whose only captures are
        RR's "Forbidden" error page)."""
        ts, orig_url = chapter_snapshots[chap_id]
        chap_url = self._ensure_https(orig_url)
        has_content, title = self._wayback_probe_chapter(chap_url, ts)
        if has_content:
            return ts, chap_url, title
        for older_ts, snap_url in self._wayback_get_chapter_snapshots(chap_url):
            if older_ts >= ts:
                continue
            snap_url = self._ensure_https(snap_url)
            has_content, title = self._wayback_probe_chapter(snap_url, older_ts)
            if has_content:
                return older_ts, snap_url, title
        return None

    @staticmethod
    def _title_from_wayback_url(url):
        """Fallback: extract a chapter title from a URL slug.
        E.g. '.../chapter/1234/101-eat-or-be-eaten' -> '101 - Eat or Be Eaten'"""
        slug_match = re.search(r'/chapter/\d+/(.+?)/?$', url)
        if not slug_match:
            return 'Chapter (Archived)'
        slug = slug_match.group(1)
        from urllib.parse import unquote
        slug = unquote(slug)
        title = slug.replace('-', ' ').strip()
        parts = title.split(' ', 1)
        if len(parts) == 2 and parts[0].isdigit():
            return '%s - %s' % (parts[0], parts[1].title())
        return title.title()

    def _wayback_get_all_snapshots(self, story_id):
        """Get all archived URLs for a story (ToC + chapters) in one CDX call.
        Returns (toc_snapshots, chapter_snapshots) where:
          toc_snapshots: list of (original_url, timestamp) for story ToC pages
          chapter_snapshots: dict of chapter_id -> (timestamp, original_url)
            (most recent per chapter)
        """
        rows = self._wayback_cdx_query(
            'www.royalroad.com/fiction/%s/*' % story_id
        )

        toc_snapshots = []
        chapter_snapshots = {}
        # Match chapter URLs with a valid slug. RR slugs are alphanumerics
        # and dashes only — anything else (e.g. percent-encoded chars from
        # broken referrer crawls) means the URL is junk and the archived
        # page is typically a 403/error page.
        chap_id_pattern = re.compile(r'/chapter/(\d+)/[a-zA-Z0-9][a-zA-Z0-9-]*$')
        # Match only clean ToC URLs (slug after story ID, no query params)
        toc_url_pattern = re.compile(
            r'https?://(?:www\.)?royalroad\.com/fiction/\d+/[a-zA-Z0-9][a-zA-Z0-9-]*$')

        for row in rows:
            original, timestamp, statuscode = row
            chap_match = chap_id_pattern.search(original)
            if chap_match:
                chap_id = chap_match.group(1)
                # Keep most recent timestamp per chapter
                if chap_id not in chapter_snapshots or timestamp > chapter_snapshots[chap_id][0]:
                    chapter_snapshots[chap_id] = (timestamp, original)
            elif toc_url_pattern.match(original):
                # Only consider clean ToC URLs (no ?review=, ?reviews=, etc.)
                toc_snapshots.append((original, timestamp))

        return toc_snapshots, chapter_snapshots

    def _wayback_fetch_raw(self, url, timestamp):
        """Fetch a page from Wayback using id_ modifier (raw, no toolbar)."""
        wayback_url = 'https://web.archive.org/web/%sid_/%s' % (timestamp, url)
        return self.get_request(wayback_url)

    def _wayback_parse_toc(self, html_data):
        """Parse an archived RR ToC page and return ordered list of chapter info.
        Each entry: {'title': str, 'url': str, 'chapter_id': str}
        """
        soup = self.make_soup(html_data)
        chapters_table = soup.find('table', {'id': 'chapters'})
        if not chapters_table:
            return []

        tbody = chapters_table.find('tbody')
        if not tbody:
            return []

        chap_pattern = re.compile(r'/chapter/(\d+)')
        result = []
        for tr in tbody.find_all('tr'):
            tds = tr.find_all('td')
            if len(tds) < 1:
                continue
            a_tag = tds[0].find('a')
            if not a_tag or not a_tag.get('href'):
                continue
            href = a_tag['href']
            chap_match = chap_pattern.search(href)
            if not chap_match:
                continue
            chapter_id = chap_match.group(1)
            title = a_tag.text.strip()
            chapter_url = 'https://' + self.getSiteDomain() + href
            entry = {
                'title': title,
                'url': chapter_url,
                'chapter_id': chapter_id,
            }
            # Try to extract date from the second td
            if len(tds) >= 2:
                with contextlib.suppress(Exception):
                    entry['date'] = self.make_date(tds[1])
            result.append(entry)
        return result

    def _wayback_recover_stub_chapters(self):
        """Recover missing chapters from Wayback Machine for a stubbed story.
        Merges archived chapters with current ToC, storing Wayback timestamps
        for chapters that need to be fetched from the archive."""
        story_id = self.story.getMetadata('storyId')

        logger.info("Story is stubbed, querying Wayback Machine for archived chapters...")

        toc_snapshots, chapter_snapshots = self._wayback_get_all_snapshots(story_id)

        if not chapter_snapshots and not toc_snapshots:
            logger.warning("No Wayback Machine data found for story %s" % story_id)
            return

        # Collect current chapter IDs
        current_chapter_ids = set(self.chapterURLIndex.keys())

        # Find chapter IDs that exist in Wayback but not in current ToC
        missing_chapter_ids = set(chapter_snapshots.keys()) - current_chapter_ids
        if not missing_chapter_ids:
            logger.info("No missing chapters found in Wayback archive")
            return

        logger.info("Found %d missing chapter(s) in Wayback archive" % len(missing_chapter_ids))

        # Fetch an archived ToC that contains missing chapters.
        # Try most recent snapshots first — accept the first one that contains
        # at least some of the missing chapter IDs.
        # toc_snapshots is list of (original_url, timestamp)
        archived_toc_chapters = []
        for toc_original_url, timestamp in sorted(toc_snapshots, key=lambda x: x[1], reverse=True):
            try:
                # Use the original URL from CDX (includes slug) so id_ fetch works
                toc_original_url = self._ensure_https(toc_original_url)
                toc_data = self._wayback_fetch_raw(toc_original_url, timestamp)
                parsed = self._wayback_parse_toc(toc_data)
                if not parsed:
                    continue
                # Check if this ToC contains any of the missing chapters
                parsed_ids = set(c['chapter_id'] for c in parsed)
                found_missing = parsed_ids & missing_chapter_ids
                if found_missing:
                    archived_toc_chapters = parsed
                    logger.debug("Using archived ToC from timestamp %s with %d chapters"
                                 " (%d of %d missing chapters found)"
                                 % (timestamp, len(parsed),
                                    len(found_missing), len(missing_chapter_ids)))
                    break
                else:
                    logger.debug("Skipping archived ToC at %s: %d chapters but"
                                 " none are missing from current ToC"
                                 % (timestamp, len(parsed)))
            except Exception as e:
                logger.debug("Failed to fetch archived ToC at %s: %s" % (timestamp, e))
                continue

        if not archived_toc_chapters:
            # Fallback: no usable archived ToC. Fetch titles from chapter pages.
            logger.warning("No archived ToC found; fetching titles from chapter pages")
            archived_toc_chapters = []
            for chap_id in sorted(missing_chapter_ids, key=int):
                if chap_id not in chapter_snapshots:
                    continue
                resolved = self._wayback_resolve_missing_chapter(
                    chap_id, chapter_snapshots)
                if not resolved:
                    logger.warning("Skipping missing chapter %s: no archived"
                                   " capture contains chapter content"
                                   " (chapter deleted from site?)" % chap_id)
                    del chapter_snapshots[chap_id]
                    continue
                ts, chap_url, title = resolved
                # Point at the capture that actually has content so the
                # download step doesn't refetch a dead capture.
                chapter_snapshots[chap_id] = (ts, chap_url)
                if not title:
                    title = self._title_from_wayback_url(chap_url)
                archived_toc_chapters.append({
                    'title': title,
                    'url': chap_url,
                    'chapter_id': chap_id,
                })

        # Build merged chapter list preserving archived order
        # Walk through archived ToC order, using current version when available
        date_format = self.getConfig("datechapter_format",
                                     self.getConfig("datePublished_format", self.dateformat))

        # Build a lookup of current chapters by their chapter_id
        current_by_id = {}
        for chap_id, idx in self.chapterURLIndex.items():
            current_by_id[chap_id] = self.chapterUrls[idx]

        # Track which current chapters we've placed in the merged list
        placed_current_ids = set()
        merged = []

        placed_wayback_ids = set()
        for archived_chap in archived_toc_chapters:
            chap_id = archived_chap['chapter_id']
            if chap_id in current_by_id:
                # Chapter still exists on the live site — use the current version
                merged.append(current_by_id[chap_id])
                placed_current_ids.add(chap_id)
            elif chap_id in chapter_snapshots:
                # Chapter was removed but exists in Wayback
                chap_meta = {
                    'title': archived_chap['title'],
                    'url': archived_chap['url'],
                }
                if 'date' in archived_chap:
                    chap_meta['date'] = archived_chap['date'].strftime(date_format)
                ts, orig_url = chapter_snapshots[chap_id]
                self.wayback_chapters[chap_id] = (ts, self._ensure_https(orig_url))
                merged.append(chap_meta)
                placed_wayback_ids.add(chap_id)

        # Add missing chapters not found in any archived ToC.
        # Fetch each chapter page to get the real title from <h1>.
        unplaced_missing = sorted(
            missing_chapter_ids - placed_wayback_ids, key=int)
        if unplaced_missing:
            logger.info("%d missing chapter(s) not in any archived ToC,"
                        " fetching titles from chapter pages"
                        % len(unplaced_missing))
            extra_chapters = []
            for chap_id in unplaced_missing:
                if chap_id not in chapter_snapshots:
                    continue
                resolved = self._wayback_resolve_missing_chapter(
                    chap_id, chapter_snapshots)
                if not resolved:
                    logger.warning("Skipping missing chapter %s: no archived"
                                   " capture contains chapter content"
                                   " (chapter deleted from site?)" % chap_id)
                    continue
                ts, chap_url, title = resolved
                if not title:
                    title = self._title_from_wayback_url(chap_url)
                chap_meta = {
                    'title': title,
                    'url': chap_url,
                }
                self.wayback_chapters[chap_id] = (ts, chap_url)
                extra_chapters.append(chap_meta)

            # Insert extra chapters in correct position by chapter ID.
            # Build a map of chapter_id -> position for the merged list,
            # then interleave extra chapters based on ID ordering.
            if extra_chapters and merged:
                # Get chapter IDs for each position in merged list
                chap_id_re = re.compile(r'/chapter/(\d+)')
                merged_ids = []
                for chap in merged:
                    m = chap_id_re.search(chap['url'])
                    merged_ids.append(int(m.group(1)) if m else 0)

                # Insert each extra chapter before the first merged chapter
                # with a higher ID
                for extra in reversed(extra_chapters):
                    m = chap_id_re.search(extra['url'])
                    extra_id = int(m.group(1)) if m else 0
                    insert_pos = len(merged)
                    for i, mid in enumerate(merged_ids):
                        if mid > extra_id:
                            insert_pos = i
                            break
                    merged.insert(insert_pos, extra)
                    merged_ids.insert(insert_pos, extra_id)
            else:
                merged.extend(extra_chapters)

        # Append any current chapters not found in the archived ToC
        # (newer chapters added after archiving)
        for chap_id in self.chapterURLIndex:
            if chap_id not in placed_current_ids:
                merged.append(current_by_id[chap_id])

        # Replace chapter list and rebuild index
        self.chapterUrls = merged
        self.chapterURLIndex = {}
        chap_pattern_long = re.compile(
            r'https?://(?:www\.)?royalroadl?\.com/fiction/\d+/[^/]+/chapter/(\d+)/[^/]+/?$')
        chap_pattern_short = re.compile(
            r'https?://(?:www\.)?royalroadl?\.com/fiction/\d+/chapter/(\d+)/?$')
        for i, chap in enumerate(self.chapterUrls):
            match = chap_pattern_long.match(chap['url']) or chap_pattern_short.match(chap['url'])
            if match:
                self.chapterURLIndex[match.group(1)] = i
        self.story.setMetadata('numChapters', self.num_chapters())

        logger.info("Merged chapter list: %d total (%d from Wayback)"
                     % (len(self.chapterUrls), len(self.wayback_chapters)))

    ## Getting the chapter list and the meta data, plus 'is adult' checking.
    def extractChapterUrlsAndMetadata(self):

        url = self.url
        logger.debug("URL: "+url)

        # Log in so site will mark the chapers as read
        self.performLogin()

        data = self.get_request(url)

        soup = self.make_soup(data)
        # print data

        # site has taken to presenting a *page* that says 404 while
        # still returning an HTTP 200 code.
        div404 = soup.find('div',{'class':'number'})
        if div404 and stripHTML(div404) == '404':
            raise exceptions.StoryDoesNotExist(self.url)

        ## Title
        title = soup.select_one('.fic-header h1').text
        self.story.setMetadata('title',title)

        # Find authorid and URL from... author url.
        mt_card_social = soup.find(None,{'class':'mt-card-social'})
        author_link = mt_card_social('a')[-1]
        if author_link:
            authorId = author_link['href'].rsplit('/', 1)[1]
            self.story.setMetadata('authorId', authorId)
            self.story.setMetadata('authorUrl','https://'+self.host+'/user/profile/'+authorId)

        self.story.setMetadata('author',soup.find(attrs=dict(property="books:author"))['content'])


        chapters = soup.find('table',{'id':'chapters'}).find('tbody')
        tds = [tr.find_all('td') for tr in chapters.find_all('tr')]

        if not tds:
            raise exceptions.FailedToDownload(
                "Story has no chapters: %s" % url)

        # Links in the RR ToC page are in the normalized long form, so match is simpler than in normalize_chapterurl()
        chap_pattern_long = r"https?://(?:www\.)?royalroadl?\.com/fiction/\d+/[^/]+/chapter/(\d+)/[^/]+/?$"
        for chapter,date in tds:
            chapterUrl = 'https://' + self.getSiteDomain() + chapter.a['href']
            chapterDate = self.make_date(date)
            format = self.getConfig("datechapter_format", self.getConfig("datePublished_format", self.dateformat))
            if self.add_chapter(chapter.text, chapterUrl, {'date': chapterDate.strftime(format)}):
                match = re.match(chap_pattern_long, chapterUrl)
                if match:
                    chapter_id = match.group(1)
                    self.chapterURLIndex[chapter_id] = len(self.chapterUrls) - 1

        description = soup.select_one('div.description div.hidden-content')
        self.setDescription(url,description)

        self.story.setMetadata('dateUpdated', self.make_date(tds[-1][1]))
        self.story.setMetadata('datePublished', self.make_date(tds[0][1]))

        for a in soup.find_all('a',{'class':'fiction-tag'}): # not all stories have genre
            genre = stripHTML(a)
            if not "Unspecified" in genre:
                self.story.addToList('genre',genre)

        for label in [stripHTML(a) for a in soup.find_all('span', {'class':'label'})]:
            if 'COMPLETED' == label:
                self.story.setMetadata('status', 'Completed')
            elif 'ONGOING' == label:
                self.story.setMetadata('status', 'In-Progress')
            elif 'HIATUS' == label:
                self.story.setMetadata('status', 'Hiatus')
            elif 'STUB' == label:
                self.story.setMetadata('status', 'Stub')
            elif 'DROPPED' == label:
                self.story.setMetadata('status', 'Dropped')
            elif 'INACTIVE' == label:
                self.story.setMetadata('status', 'Inactive')
            elif 'Fan Fiction' == label:
                self.story.addToList('category', 'FanFiction')
            elif 'Original' == label:
                self.story.addToList('category', 'Original')

        # 'rating' in FFF speak means G, PG, Teen, Restricted, etc.
        # 'stars' is used instead for RR's 1-5 stars rating.
        stars=soup.find(attrs=dict(property="books:rating:value"))['content']
        self.story.setMetadata('stars',stars)
        logger.debug("stars:(%s)"%self.story.getMetadata('stars'))

        warning = soup.find('strong',string='Warning')
        if warning != None:
            for li in warning.find_next('ul').find_all('li'):
                self.story.addToList('warnings',stripHTML(li))

        # get cover
        img = soup.find(None,{'class':'row fic-header'}).find('img')
        if img:
            cover_url = img['src']
            # usually URL is for thumbnail. Try expected URL for larger image, if fails fall back to the original URL
            cover_set = self.setCoverImage(url,cover_url.replace('/covers-full/', '/covers-large/'))[0]
            if not cover_set or cover_set.startswith("failedtoload"):
                self.setCoverImage(url,cover_url)
                    # some content is show as tables, this will preserve them

        itag = soup.find('i',title='Story Length')
        if itag and itag.has_attr('data-content'):
            # "calculated from 139,112 words"
            m = re.search(r"calculated from (?P<words>[0-9,]+) words",itag['data-content'])
            if m:
                self.story.setMetadata('numWords',m.group('words'))

        # Recover missing chapters from Wayback Machine for stubbed stories
        if (self.story.getMetadata('status') == 'Stub'
                and self.getConfig('use_wayback_for_stubs', False)):
            try:
                self._wayback_recover_stub_chapters()
            except Exception as e:
                logger.warning("Wayback Machine recovery failed: %s" % e)

    def _wayback_get_chapter_snapshots(self, chapter_url):
        """Get all archived captures for a specific chapter (newest first),
        as (timestamp, original_url) pairs. Matches across slug variants.
        Used on-demand when we need to fall back to an older version."""
        chap_match = re.search(r'/fiction/(\d+)/[^/]+/chapter/(\d+)', chapter_url)
        if not chap_match:
            return []
        story_id, chapter_id = chap_match.group(1), chap_match.group(2)
        # CDX only supports prefix wildcards (a mid-URL '*' matches
        # nothing), so query the whole story and filter server-side for
        # this chapter's captures across all slug variants.
        rows = self._wayback_cdx_query(
            'www.royalroad.com/fiction/%s/*' % story_id,
            extra_filter='original:.*/chapter/%s/.*' % chapter_id,
            collapse=False)
        # (timestamp, original_url) pairs, newest first, so a fallback
        # fetch uses the URL the capture was actually archived under.
        snapshots = sorted(set((row[1], row[0]) for row in rows), reverse=True)
        return snapshots

    def _wayback_fetch_best_chapter(self, url, newest_ts):
        """Fetch chapter from Wayback using newest_ts. If the content looks
        like a stub (less than 5% of an older version's content), falls back
        to the older version."""
        data = self._wayback_fetch_raw(url, newest_ts)
        soup = self.make_soup(data)
        div = soup.find('div', {'class': "chapter-inner chapter-content"})

        # A redirect capture, or a capture with no content div (e.g. RR's
        # "Forbidden" page for a deleted chapter, which RR serves with
        # HTTP 200 and Wayback archives as a 200), is useless — fall back
        # to older snapshots.
        if self._is_wayback_redirect_capture(data) or div is None:
            logger.debug("Newest Wayback snapshot (%s) for chapter has no"
                         " usable content, trying older versions" % newest_ts)
            for ts, snap_url in self._wayback_get_chapter_snapshots(url):
                if ts >= newest_ts:
                    continue
                try:
                    cand_data = self._wayback_fetch_raw(
                        self._ensure_https(snap_url), ts)
                except Exception:
                    continue
                if self._is_wayback_redirect_capture(cand_data):
                    continue
                cand_soup = self.make_soup(cand_data)
                cand_div = cand_soup.find('div', {'class': "chapter-inner chapter-content"})
                if cand_div is None:
                    continue
                logger.info("Using older Wayback snapshot (%s) for chapter"
                            % ts)
                soup, div, newest_ts = cand_soup, cand_div, ts
                break

        # If content is suspiciously short (under ~500 words), check if an
        # older archived version has substantially more content
        if div:
            newest_text = div.get_text()
            newest_len = len(newest_text)
            newest_words = len(newest_text.split())
            if newest_words < 500:
                all_snapshots = self._wayback_get_chapter_snapshots(url)
                # Find captures older than the one we just fetched
                older_snapshots = [(ts, u) for ts, u in all_snapshots
                                   if ts < newest_ts]
                if older_snapshots:
                    older_ts, older_url = older_snapshots[0]  # most recent older version
                    try:
                        older_data = self._wayback_fetch_raw(
                            self._ensure_https(older_url), older_ts)
                        older_soup = self.make_soup(older_data)
                        older_div = older_soup.find('div', {'class': "chapter-inner chapter-content"})
                        if older_div:
                            older_len = len(older_div.get_text())
                            if older_len > 0 and newest_len < older_len * 0.05:
                                logger.info("Newest version (%s) has only %d chars"
                                            " vs %d in older version (%s);"
                                            " using older version"
                                            % (newest_ts, newest_len,
                                               older_len, older_ts))
                                return older_soup
                    except Exception:
                        pass  # Older version failed, stick with newest

        return soup

    # grab the text for an individual chapter.
    def getChapterText(self, url):

        logger.debug('Getting chapter text from: %s' % url)

        # Check if this chapter needs to be fetched from the Wayback Machine
        chapter_id = None
        wayback_ts = None
        chap_match = re.search(r'/chapter/(\d+)', url)
        if chap_match:
            chapter_id = chap_match.group(1)

        if chapter_id and chapter_id in self.wayback_chapters:
            wayback_ts, archived_url = self.wayback_chapters[chapter_id]
            logger.info("Fetching chapter %s from Wayback Machine (timestamp %s)"
                        % (chapter_id, wayback_ts))
            # archived_url may use a different slug than the URL stored in
            # chapterUrls (which can come from an archived ToC) — the
            # chapter itself was only ever archived under archived_url.
            soup = self._wayback_fetch_best_chapter(archived_url, wayback_ts)
        else:
            soup = self.make_soup(self.get_request(url))

        div = soup.find('div',{'class':"chapter-inner chapter-content"})

        # TODO: these stories often have tables in, but these wont render correctly
        # defaults.ini output CSS now outlines/pads the tables, at least.

        if None == div:
            if wayback_ts:
                # Wayback-recovery chapter with no recoverable content in
                # any capture (e.g. deleted from RR before it was ever
                # crawled with content) — insert a placeholder rather than
                # aborting the whole download.
                logger.warning("No archived capture of chapter %s contains"
                               " content; inserting placeholder" % url)
                return ('<div><p><i>(Chapter content unavailable: this'
                        ' chapter was removed from Royal Road and no'
                        ' Wayback Machine capture of it contains its'
                        ' content.)</i></p></div>')
            raise exceptions.FailedToDownload("Error downloading Chapter: %s!  Missing required element!" % url)

        if self.getConfig("include_author_notes",True):
            # collect both first, changing div for frontnote first
            # causes confusion in the tree.
            frontnote = div.find_previous('div', {'class':'author-note-portlet'})
            endnote = div.find_next('div', {'class':'author-note-portlet'})
            if frontnote:
                # move frontnote into chapter text div.
                div.insert(0,frontnote.extract())
            if endnote:
                # move endnote into chapter text div.
                div.append(endnote.extract())
        def has_display_none_style(tag):
            tag_class = tag.get('class', '')
            return any(style in tag_class for style in self.styles_to_ignore)

        for element in div.find_all(has_display_none_style):
            element.extract()

        # Use a fetch wrapper that handles Wayback fallback for failed
        # images and compresses large animated GIF/WebP to first frame
        return self.utf8FromSoup(url, div,
                                 fetch=self._make_image_fetch(wayback_ts))
