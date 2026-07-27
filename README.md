# Local Cover Art

A [DroppedNeedle](https://github.com/DroppedNeedle/DroppedNeedle) plugin that
serves the cover art already in your library — embedded in the tracks, or a
`cover.jpg` sitting beside them — for the albums the online providers cannot
supply.

## Why

DroppedNeedle looks album art up from online providers keyed on a MusicBrainz
release-group MBID. That leaves two holes:

- **Unidentified albums have no key.** If DroppedNeedle could not match an
  album to MusicBrainz, `TargetCoverArtService.get_release_group_cover` returns
  before a provider is asked, and the client shows the turntable placeholder —
  even when every file in the album carries a front cover.
- **Identified albums can still miss.** The Cover Art Archive has nothing for
  plenty of release groups. `CoverArtRepository` carries an embedded-art last
  resort for this case, but the native catalog composition builds it with
  `library_db=None` (see `get_target_coverart_repository`), so the fallback
  returns at its first guard and never opens a file.

On the library this was written for, 18% of albums (487 of 2,733) were
unidentified and therefore *permanently* placeholder-only, while 82% of albums
had embedded art in their files and 391 album folders had a `cover.jpg`. The
art was there the whole time; nothing would read it.

## What it does

Wraps `TargetCoverArtService.get_release_group_cover` and its etag twin. When
the normal provider path comes back empty, it looks for artwork in the album's
own files:

1. embedded front cover in the tracks (up to *Tracks to scan per album*), then
2. `cover` / `folder` / `front` / `album` / `albumart` / `artwork`
   (`.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`) in the album's directory.

What it finds is cached in DroppedNeedle's own cover cache under a separate
`local_<id>` key, so the host's provider covers are never overwritten. Albums
with no local art are remembered for six hours so a miss does not re-open every
file on the next request.

It works for both the web UI and OpenSubsonic clients (Symfonium, and friends),
because both are served by the same cover service.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| Prefer local art over the online providers | off | On: art in your own files wins and the providers are never asked. Turn this on if your tags are curated and you want exactly the artwork you filed. |
| Also use folder images | on | Off: embedded art only, `cover.jpg` and friends ignored. |
| Tracks to scan per album | 12 | How many of an album's files to open looking for embedded art before giving up. |

## Install

In DroppedNeedle: **Settings → Plugins**, paste

```
https://github.com/TheSmuks/dn-local-cover-art
```

then enable it. Installing stores code; enabling is what runs it.

Under Docker, `/app/plugins` is **not** mounted by default — add a volume for
it or every plugin you install disappears when the container is recreated:

```yaml
volumes:
  - ${CONFIG_PATH}/droppedneedle/plugins:/app/plugins
```

Your music library must be readable by DroppedNeedle, which it already is if
the library scan works. Nothing here writes to your music files.

## Caveats, honestly

- **This patches host internals.** `api_version = 0` has no cover-art
  capability, so there is no supported extension point to hang this off. The
  plugin declares the reserved `metadata_provider` capability (which the host
  validates and then skips) and does its work at construction. If a
  DroppedNeedle release moves `TargetCoverArtService` or renames those two
  methods, the plugin refuses to load and says so in Settings → Plugins rather
  than half-working. Re-check it after upgrading.
- **Disabling takes up to 30 seconds**, or a restart. The host leaves the
  module patched after an admin disables a plugin, so the wrapper re-reads its
  own enabled flag on a timer and steps aside when it is off.
- **It is a fallback, not a fix for identification.** An unidentified album
  gets its cover back, but it stays unidentified — no MusicBrainz metadata, no
  release matching. Working the identification review queue is still worth it.
- A plugin runs in-process with your server's full privileges and there is no
  sandbox. Read the code — it is one file.

## Licence

AGPL-3.0, matching DroppedNeedle.
