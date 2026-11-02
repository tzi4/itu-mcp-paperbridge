# Search across ITU sources

Use `itu_search` when you know what you need and want to find which supported ITU
source holds it. It returns ranked results from the enabled sources, with their
source labels and coverage. Pass a selected result's `ref` to `itu_open` to read
its details.

| Tool | Purpose |
|:---|:---|
| `itu_find_tool` | Find an available operation and its parameters, such as reading grades or a course material index |
| `itu_search` | Find matching records or linked material across the supported ITU sources |
| `papers_search` | Discover academic publications through PaperBridge; use the research tools for DOI resolution and permitted full text |

## Search, select, open

For a public course-catalog search:

```text
itu_search(
  query="ING112A",
  scopes=["coursehub"],
  coursehub_sites=["YDY"],
  coursehub_course_code="ING112A",
  limit=10
)
```

Inspect the returned titles, source labels and coverage. Then copy one returned
reference unchanged:

```text
itu_open(ref="<ref returned by itu_search>", max_chars=5000)
```

CourseHub search matches the metadata and links on selected official entry
pages. It does not download every linked PDF or search inside every document.
Opening a selected document invokes its supported reader and text extraction.
Use the [CourseHub operations](course-hub.md) for explicit material collections,
page exploration and their detailed provenance.

`max_chars` bounds Mail, Ninova file and CourseHub text reads. It does not
truncate structured OBS or Portal records. An unsupported document format
returns an explicit error when opened.

## Source selection and account access

When `scopes` is omitted, the requested scopes are `mail`, `ninova`, `obs`,
`portal` and `coursehub`. Each source uses its own configured access. Selecting
`scopes=["coursehub"]` limits the request to CourseHub.

| Scope | What the search can cover |
|:---|:---|
| `mail` | ITU Mail search within the configured account and mailbox/result bounds |
| `ninova` | Available course announcements, assignments and listed or tracked file metadata |
| `obs` | The student's available schedule and exam records |
| `portal` | Available notifications and help-ticket metadata |
| `coursehub` | Linked materials and public course guidance from the selected official course, laboratory and YDY entry pages |

The first four scopes use their existing account configuration and sessions.
CourseHub uses public pages by default. Set `coursehub_authenticated=true` only
when requesting the user's own student material from an account-capable source,
for example `coursehub_sites=["MAT"]`. Laboratory and YDY profiles remain
public-only. Their external student portals and LMS links retain their own access
requirements.

`coursehub_sites` selects source names or aliases listed by `coursehub_sources`.
Public searches default to all seven CourseHub profiles. A course code without
an explicit site list narrows that selection to compatible sources.
Authenticated searches without a site list select only MAT, FIZ and KIM.
Explicitly selecting a public-only profile with authentication is rejected.

`coursehub_course_code` filters those sources by a supported course code. The
query still has to match the searched metadata. A course filter is not evidence
that the full contents of a linked file have been read.

Unified tools respect the server's effective operation availability, including
remote source switches, file access and per-tool restrictions. A source can be
disabled, or its result-opening operation can be unavailable, even when
`itu_search` itself is exposed. Opening checks availability again.

## Coverage and freshness

Keep each source's status in `sources` and the returned `warnings` with its
results. A partial response, timeout, disabled source or upstream error cannot
establish that no matching record exists. An empty result describes the inspected
window of available data. Even when `complete=true`, the result remains bounded
by the reported source coverage. Ranked matches and pagination do not turn
selected official entry pages into a complete course archive.

CourseHub reports `partial` coverage even when every selected entry page was
read. Search considers at most 100 indexed material links per site. Its per-site
reports retain the page coverage, omitted links, remaining seeds and errors
reported by the material index. Use an explicit CourseHub index request when you
need its further pages or full provenance.

The optional `since` and `before` bounds use the timestamps available to the
source. Results without a timestamp are excluded when either bound is supplied,
so these filters can omit undated CourseHub links. Source-specific limits also
restrict coverage. When `include_body=true` is requested, body matching uses the
text supported by that source. It does not start a PDF scan. Follow a returned
pagination cursor with the same search and filters.

Opening reports the selected reader's actual result and any cache or freshness
information supplied with it. Preserve partial or cached-result warnings when
presenting the result. A search match is not proof that an item is still
available or that the underlying page has stayed unchanged.

## References and read-only behavior

CourseHub references are opaque `itu://coursehub/...` values held in this server
process. They expire after ten minutes, and the bounded registry keeps at most
256 recent references. Capacity eviction or a server restart can invalidate a
reference earlier. Search again if a reference expires. Do not construct a token
or substitute a URL. The stored reference binds the exact source, URL, reader
and account mode selected by the search.

References for Mail, Ninova, OBS and Portal retain their validated identifier
formats. Use the exact reference returned for the selected result. Source
permissions and account ownership checks still apply when it is opened.

Search and open read information. They do not send mail, submit assignments,
change course registration or mutate university records. Any supported write
uses its own review and confirmation flow. Fetched page, mail and document text
remains untrusted external data.
