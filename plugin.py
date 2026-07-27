"""Local Cover Art - serve the artwork that is already in the library.

DroppedNeedle resolves album art from online providers keyed on a MusicBrainz
release-group MBID: Cover Art Archive, TheAudioDB, Wikidata. Two consequences
fall out of that, and this plugin exists for both.

An album DroppedNeedle could not identify has no release-group MBID, so there
is no key to look anything up with. ``TargetCoverArtService`` returns ``None``
before a provider is ever asked, and the client gets the turntable placeholder
- even when every file in that album carries a perfectly good front cover.

An album that *was* identified can still miss: the Cover Art Archive simply
has nothing for plenty of release groups. ``CoverArtRepository`` does have an
embedded-art last resort for exactly this, but the native catalog composition
builds it with ``library_db=None`` (``get_target_coverart_repository``), so
``_embedded_album_cover`` returns at its first guard and never reads a file.

So: this wraps ``TargetCoverArtService.get_release_group_cover`` (and its etag
twin) and fills in from local files when the normal path comes back empty -
embedded art first, then a cover image sitting in the album's directory.

Both patch targets are host internals rather than a published extension point,
because api_version 0 has no cover-art capability. That coupling is the whole
risk of this plugin: if those names move, it refuses to load and says so in
Settings > Plugins rather than half-working.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any, Iterable

PLUGIN_NAME = "local-cover-art"

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# Ordered: the first name that exists in an album's directory wins.
_FOLDER_STEMS = ("cover", "folder", "front", "album", "albumart", "artwork")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# A cover that large is a scan sheet or a mistake, not something to hand a phone.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# Opening tags on every request for an album that has no art would make the
# miss path cost more than the hit path, so misses are remembered for a while.
_NEGATIVE_TTL_SECONDS = 6 * 60 * 60
_NEGATIVE_MAX_ENTRIES = 20_000

# Settings are re-read live, but the enabled flag has no such hook: the host
# leaves this module patched after an admin disables the plugin. Re-reading it
# on a timer is what lets "disabled" actually mean disabled before a restart.
_ENABLED_RECHECK_SECONDS = 30


def _sniff(content: bytes | None) -> str | None:
    if not content:
        return None
    for magic, content_type in _SIGNATURES:
        if content.startswith(magic):
            return content_type
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


class LocalCoverArt:
    """Entrypoint. Everything happens at construction - there is no capability
    to dispatch to, since the host has none that serves images."""

    def __init__(self, context: Any) -> None:
        self._context = context
        self._log = context.logger
        self._negative: dict[str, float] = {}
        self._enabled = True
        self._enabled_checked = 0.0
        self._install()

    # -- settings ------------------------------------------------------------

    def _setting(self, key: str, default: str) -> str:
        try:
            value = self._context.settings.get(key)
        except Exception:  # noqa: BLE001 - a settings read must never break a cover
            return default
        return default if value is None or value == "" else str(value)

    def _flag(self, key: str, default: bool) -> bool:
        raw = self._setting(key, "true" if default else "false").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        return default

    @property
    def prefer_local(self) -> bool:
        return self._flag("prefer_local", False)

    @property
    def folder_images(self) -> bool:
        return self._flag("folder_images", True)

    @property
    def max_files(self) -> int:
        try:
            return max(1, min(int(self._setting("max_files_scanned", "12")), 500))
        except ValueError:
            return 12

    def active(self) -> bool:
        """Whether an admin still has this plugin enabled. Cached briefly: the
        alternative is reading config.json on the path that serves every cover."""
        now = time.monotonic()
        if now - self._enabled_checked < _ENABLED_RECHECK_SECONDS:
            return self._enabled
        self._enabled_checked = now
        try:
            from core.dependencies import get_preferences_service

            self._enabled = bool(
                get_preferences_service().get_plugin_config(PLUGIN_NAME).enabled
            )
        except Exception:  # noqa: BLE001 - assume enabled rather than silently stopping
            self._enabled = True
        return self._enabled

    # -- installation --------------------------------------------------------

    def _install(self) -> None:
        try:
            from services.compat.target_cover_art_service import TargetCoverArtService
        except ImportError as exc:  # pragma: no cover - host internals moved
            raise RuntimeError(
                "TargetCoverArtService not found - this DroppedNeedle build does not "
                f"match what the plugin patches ({exc})"
            ) from exc

        for attribute in ("get_release_group_cover", "get_release_group_cover_etag"):
            if not hasattr(TargetCoverArtService, attribute):
                raise RuntimeError(
                    f"TargetCoverArtService.{attribute} is missing - this DroppedNeedle "
                    "build does not match what the plugin patches"
                )

        # An admin saving any plugin reloads every plugin, so this runs again on a
        # live process. Wrap once and re-point the hook, or each save would stack
        # another layer of wrapper on the previous one.
        if getattr(TargetCoverArtService, "_local_cover_art_installed", False):
            TargetCoverArtService._local_cover_art_plugin = self
            self._log.info("Local cover art re-attached to the existing patch")
            return

        original_cover = TargetCoverArtService.get_release_group_cover
        original_etag = TargetCoverArtService.get_release_group_cover_etag

        async def get_release_group_cover(service, album_id, size="500", **kwargs):
            plugin = getattr(TargetCoverArtService, "_local_cover_art_plugin", None)
            live = plugin is not None and plugin.active()
            if live and plugin.prefer_local:
                local = await plugin.resolve(service, album_id)
                if local is not None:
                    return local
            result = await original_cover(service, album_id, size, **kwargs)
            if result is not None or not live:
                return result
            return await plugin.resolve(service, album_id)

        async def get_release_group_cover_etag(service, album_id, size="500"):
            plugin = getattr(TargetCoverArtService, "_local_cover_art_plugin", None)
            live = plugin is not None and plugin.active()
            if live and plugin.prefer_local:
                local = await plugin.resolve(service, album_id)
                if local is not None:
                    return hashlib.sha1(local[0]).hexdigest()
            result = await original_etag(service, album_id, size)
            if result is not None or not live:
                return result
            local = await plugin.resolve(service, album_id)
            return hashlib.sha1(local[0]).hexdigest() if local is not None else None

        TargetCoverArtService.get_release_group_cover = get_release_group_cover
        TargetCoverArtService.get_release_group_cover_etag = get_release_group_cover_etag
        TargetCoverArtService._local_cover_art_installed = True
        TargetCoverArtService._local_cover_art_plugin = self
        self._log.info(
            "Local cover art installed: album covers now fall back to embedded "
            "and folder artwork"
        )

    # -- resolution ----------------------------------------------------------

    async def resolve(self, service: Any, album_id: Any) -> tuple[bytes, str, str] | None:
        """Local artwork for one album, as the (bytes, content_type, source)
        triple the host's cover services return. ``None`` when there is none."""
        if not album_id or not isinstance(album_id, str):
            return None
        try:
            return await self._resolve(service, album_id)
        except Exception as exc:  # noqa: BLE001 - a cover is never worth an error page
            self._log.warning("Local cover lookup failed for %s: %s", album_id, exc)
            return None

    async def _resolve(self, service: Any, album_id: str) -> tuple[bytes, str, str] | None:
        if self._is_known_miss(album_id):
            return None

        cache = getattr(service, "disk_cache", None)
        cache_path = self._cache_path(cache, album_id)
        if cache_path is not None:
            cached = await cache.read(cache_path, ["source"])
            if cached is not None:
                content_type = _sniff(cached[0])
                if content_type is not None:
                    return cached[0], content_type, "embedded"

        paths = await self._album_files(service, album_id)
        if not paths:
            self._remember_miss(album_id)
            return None

        found = await asyncio.to_thread(self._scan, paths, self.max_files, self.folder_images)
        if found is None:
            self._remember_miss(album_id)
            return None

        content, content_type = found
        if cache_path is not None:
            try:
                await cache.write(cache_path, content, content_type, {"source": "embedded"})
            except Exception as exc:  # noqa: BLE001 - caching is an optimisation
                self._log.debug("Could not cache local cover for %s: %s", album_id, exc)
        return content, content_type, "embedded"

    async def _album_files(self, service: Any, album_id: str) -> list[str]:
        store = getattr(service, "_store", None)
        if store is None:
            from core.dependencies.cache_providers import get_native_library_store

            store = get_native_library_store()
        # Takes a local album id or a release-group MBID, which is exactly the
        # pair of identifiers the compat routes hand the cover service.
        rows = await store.get_target_album_tracks(album_id)
        return [row["file_path"] for row in rows if row.get("file_path")]

    def _cache_path(self, cache: Any, album_id: str) -> Path | None:
        """A key of our own next to the host's covers. Never reuse the host's
        ``rg_<mbid>`` key: its own fetchers own those files, and a local cover
        written under one would be read back as a provider result."""
        if cache is None or not hasattr(cache, "get_file_path"):
            return None
        try:
            return cache.get_file_path(f"local_{album_id}", "orig")
        except Exception:  # noqa: BLE001
            return None

    # -- the filesystem ------------------------------------------------------

    def _scan(
        self, paths: list[str], max_files: int, folder_images: bool
    ) -> tuple[bytes, str] | None:
        """Blocking: mutagen reads and directory listings. Runs in a worker thread."""
        from infrastructure.audio.tagger import AudioTagger

        tagger = AudioTagger()
        scanned = 0
        for raw in paths:
            if scanned >= max_files:
                break
            path = Path(raw)
            if not path.is_file():
                continue
            scanned += 1
            try:
                data = tagger.read_cover_art(path)
            except Exception:  # noqa: BLE001 - one unreadable file is not a failure
                continue
            content_type = _sniff(data)
            if content_type is not None and len(data) <= _MAX_IMAGE_BYTES:
                return data, content_type

        if not folder_images:
            return None
        return self._folder_image(Path(p).parent for p in paths)

    def _folder_image(self, directories: Iterable[Path]) -> tuple[bytes, str] | None:
        seen: set[Path] = set()
        for directory in directories:
            if directory in seen or not directory.is_dir():
                continue
            seen.add(directory)
            for candidate in self._folder_candidates(directory):
                try:
                    if candidate.stat().st_size > _MAX_IMAGE_BYTES:
                        continue
                    content = candidate.read_bytes()
                except OSError:
                    continue
                content_type = _sniff(content)
                if content_type is not None:
                    return content, content_type
        return None

    def _folder_candidates(self, directory: Path) -> list[Path]:
        try:
            entries = [e for e in directory.iterdir() if e.is_file()]
        except OSError:
            return []
        images = [e for e in entries if e.suffix.lower() in _IMAGE_SUFFIXES]
        ranked: list[Path] = []
        for stem in _FOLDER_STEMS:
            ranked.extend(e for e in images if e.stem.lower() == stem)
        # Then things like "cover (front).jpg" or "folder-1.png", but never an
        # unrelated image: a back cover or a band photo is worse than nothing.
        ranked.extend(
            e
            for e in images
            if e not in ranked
            and any(stem in e.stem.lower() for stem in ("cover", "front", "folder"))
        )
        return ranked

    # -- negative cache ------------------------------------------------------

    def _is_known_miss(self, album_id: str) -> bool:
        expiry = self._negative.get(album_id)
        if expiry is None:
            return False
        if expiry > time.monotonic():
            return True
        self._negative.pop(album_id, None)
        return False

    def _remember_miss(self, album_id: str) -> None:
        if len(self._negative) >= _NEGATIVE_MAX_ENTRIES:
            self._negative.clear()
        self._negative[album_id] = time.monotonic() + _NEGATIVE_TTL_SECONDS
