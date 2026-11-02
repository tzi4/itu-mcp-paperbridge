# CourseHub

CourseHub reads ITU's mathematics, physics and chemistry pool course sites,
public laboratory guidance, YDY language program pages and the Faculty of
Science and Letters hub. Public requests use a separate
transport without account cookies. Student sessions stay in memory and are
separate for each site.

| Site | Address | Student login |
|:---|:---|:---|
| Mathematics (`MAT`) | https://mathavuz.itu.edu.tr/ | ITU SSO |
| Physics (`FIZ`) | https://fizikhavuz.itu.edu.tr/ | ITU SSO adapter, requires site verification |
| Chemistry (`KIM`) | https://kimyahavuz.itu.edu.tr/ | ITU username and student number |
| Faculty hub (`FEF`) | https://fefhavuz.itu.edu.tr/ | Public portal |
| Physics laboratory (`FIZLAB`) | https://fizik.itu.edu.tr/havuzdersleri/fiz-10xel-laboratuvar | Public laboratory guidance and materials |
| Chemistry laboratory (`KIMLAB`) | https://kimya.itu.edu.tr/kimya-havuz/kim-101el | Public laboratory guidance and materials |
| Languages (`YDY`) | https://ydy.itu.edu.tr/ | Public program guidance and materials |



## Laboratory sources

Use `site="FIZLAB"` or `site="KIMLAB"` for the public physics and chemistry
laboratory pages. They expose the instructions, announcements and material links
published by the departments. Use the exact published course code, including its
`L` or `EL` suffix. Documents and archived pages keep their actual
course and term labels. Historical material is a reference for its stated term.
Check the source page before treating it as the instructions for a current class.
Laboratory sources do not sign into student systems or submit reports.

## Language programs

Use `site="YDY"` for the School of Foreign Languages. An optional course filter
selects verified program pages, for example `course_code="ING112A"` or
`course_code="ALM101"`. Preparatory English guidance is included when exploring
the language source without a numbered course filter.

| Program or language | Course family | Public entry point |
|:---|:---|:---|
| Advanced English | `ING` | [ING 100](https://ydy.itu.edu.tr/programlar/i-leri-i-ngilizce-programi/ing-100), [ING 112A](https://ydy.itu.edu.tr/programlar/i-leri-i-ngilizce-programi/ing-112a), [ING 201A](https://ydy.itu.edu.tr/programlar/i-leri-i-ngilizce-programi/ing-201a), [ING 103 electives](https://ydy.itu.edu.tr/programlar/i-leri-i-ngilizce-programi/ing-103) |
| Preparatory English | Program guidance | [Program](https://ydy.itu.edu.tr/programlar/lisans-hazirlik-programi/program-hakkinda), [student handbook guidance](https://ydy.itu.edu.tr/programlar/lisans-hazirlik-programi/ogrenci-el-kitabi), [proficiency exam guidance](https://ydy.itu.edu.tr/programlar/lisans-hazirlik-programi/yeterlik-sinavi) |
| German | `ALM` | [German program](https://ydy.itu.edu.tr/programlar/secmeli-diller/almanca) |
| Arabic | `ARB` | [Arabic program](https://ydy.itu.edu.tr/programlar/secmeli-diller/arapca) |
| Chinese | `CIN` | [Chinese program](https://ydy.itu.edu.tr/programlar/secmeli-diller/cince) |
| French | `FRA` | [French program](https://ydy.itu.edu.tr/programlar/secmeli-diller/fransizca) |
| Spanish | `ISP` | [Spanish program](https://ydy.itu.edu.tr/programlar/secmeli-diller/ispanyolca) |
| Italian | `ITA` | [Italian program](https://ydy.itu.edu.tr/programlar/secmeli-diller/italyanca) |
| Japanese | `JPN` | [Japanese program](https://ydy.itu.edu.tr/programlar/secmeli-diller/japonca) |
| Russian | `RUS` | [Russian program](https://ydy.itu.edu.tr/programlar/secmeli-diller/rusca) |
| Turkish for international students | `YTO` | [Turkish program](https://ydy.itu.edu.tr/programlar/secmeli-diller/turkce) |
| Korean | `KOR` | [Official announcement](https://ydy.itu.edu.tr/haber-detay/2026/09/28/korece) and [Ninova course catalog](https://ninova.itu.edu.tr/tr/dersler/yabanci-diller-yuksekokulu/) |

The Korean entry is an announcement and an LMS handoff. It does not establish a
separate Korean course portal. Course families are filters within `YDY`, rather
than separate login systems.

YDY publishes program descriptions and catalog PDFs. Some linked catalog PDFs
are hosted on `ardek.itu.edu.tr`; CourseHub reads only its verified material paths.
The linked documents keep their actual titles and dates. An older catalog form
is not a current assignment or proof that every section uses the same material.

Actual class announcements, materials and assignment uploads use the student's
Ninova class, as described in the [Advanced English rules](https://ydy.itu.edu.tr/docs/librariesprovider95/default-document-library/fall-2025-aep-rules-and-regulations.pdf?sfvrsn=e7b618a_2).
The preparatory handbook page also directs students to their own Ninova files.
Use the existing Ninova tools for those records and reviewed submissions.
Elective enrollment uses the university's course-registration process.

The separate [student status portal](https://ydydurum.itu.edu.tr/ydyo-ogrenci/)
retains its own login. CourseHub's YDY source covers public information and does
not sign into that portal or perform student lookups. Other assessment platforms
and unsupported legacy links remain handoffs to their actual sites.

## Reading

Find the operations with `itu_find_tool` using `domain="coursehub"`, or call them
directly in the full tool profile.

1. `coursehub_sources` lists the sources, verified entry points and handoffs. Its
   `capabilities` flags are `public_read`, `account_read` and `reviewed_write`. It
   also reports
   the account and write configuration.
   `session_cached` reports whether the connector holds a session. A
   `session_status` of `cached_not_revalidated` describes a stored session. Use an
   account read to verify current access. `not_cached` means no session is held.
2. `coursehub_materials(site="MAT", course_code="MAT103E", authenticated=true,
   collection="slides", offset=0, limit=30)` returns a compact, paginated index of
   linked materials. Collections are `all`, `slides`, `practice`, `exams`,
   `syllabi` and `guides`. Choose a collection and inspect its titles, course labels
   and exact URLs before opening a document. `limit` is 1 to 100 and `offset` is
   0 to 10,000. Exact matches rank first. Shared, general and unknown material
   retains its actual classification when a course filter is supplied.
3. Use `coursehub_page` for linked HTML syllabus or catalog pages and
   `coursehub_text` for supported files, passing the exact returned URL and the
   same account setting when the resource requires login. Mathematics links such as
   `show_slide.php?s=MAT103Ech01` can return a PDF through a native page endpoint.
   The reader checks the actual response type and file signature. It extracts
   PDF, DOCX, PPTX, TXT and CSV. PPTX extraction keeps presentation order and
   includes speaker notes. Documents are limited to 25 MiB. Office packages are
   limited to 2,000 entries and 32 MiB of expanded data. Parsing runs in a separate
   process with a stripped environment and a 30-second deadline. Linux also
   enforces memory and CPU limits.
4. `coursehub_explore(site="MAT", course_code="MAT103E", max_pages=5)` follows a
   bounded set of same-site page links for announcements and broader discovery.
   It returns sections, tables, resources and forms. Shared labels such as
   `MAT103/E` cover both language variants. Without a course filter, exploration
   follows all discovered course links within the page bound. A code match alone
   does not establish a document's relevance.
5. `coursehub_page` reads one official page, including announcements, tables,
   syllabus pages, course content and the fields of forms the page offers.
   Mathematics syllabus buttons are resolved from their actual GET form values.
   Forms report editable fields and their available options. `hidden_fields`
   contains names and types only, with hidden values omitted. `kind="read_selector"`
   identifies a verified mathematics syllabus GET selector and has
   `requires_reviewed_write_rule=false`. Other forms have `kind="unverified_form"`.
   Their `requires_reviewed_write_rule` is `true` for POST and `null` for an
   unverified GET form. An unverified form is not a supported write action.

The material index reads the selected source's configured entry pages. The
original pool sites use one public or student page with
`coverage="root_links_only"`. Laboratory and language sources use a bounded set
of verified entry pages and report `coverage="seed_pages_only"`. A course filter
can select the relevant language program pages. The index does not crawl deeper
pages or download the linked files. New public sources report `source_urls`,
`sources` and `entry_points` for their observed provenance. Their `pages_read`,
`pages_attempted`, `page_limit`, `seeds_total`, `remaining_seeds` and
`remaining_seed_urls` describe the bounded fetch. The seed-page limit is 20.
Check `partial`, `errors` and aggregated `omitted` before interpreting the counts.
If every seed fails, the operation returns a source error. Login on
account-capable sources can require additional verification requests.

Each item keeps the source title and URL, its link `kind`, `readable` and
`pool_site` flags, material `collection`, `course_codes` and `course_match`.
`text_extraction_supported` describes inferred format support: `true` for known
text formats or recognized native PDF routes, `false` for video, archives and
legacy Office formats, and `null` when unknown. Actual reads still verify the
response and file signature. Unsupported known formats are rejected before any
login or download; their links remain available for opening in a browser.
`collection_basis` identifies a verified native endpoint, a title or extension
classification, an unclassified link or conflicting source labels. Course and
collection labels describe index evidence. Document contents come from a separate
read.
`collection=null` means no single collection is established. A readable link can still
require authentication or fail at the source. Use the actual read result to
verify access. A recognized native academic identifier may appear as
`native_identifier`. Mathematics filenames with the internal `T` language marker
map to canonical codes such as `MAT103`, while the `E` variant remains `MAT103E`.
Course labels distinguish `exact_course`, `shared_course` and `general_or_unknown`.
Without a requested course, recognized codes use `identified_course`.

Seeded indexes deduplicate the same material URL while preserving all observed
`source_urls`, `titles` and `provenance`, including archival labels.
`source_course_codes` records entry-page context and does not establish an exact
document association. If known collection labels conflict, `collection` is
`null`, `collection_basis` is `conflicting_labels`, `collections` retains the
observed categories and `collection_ambiguous` is `true`. Such material can appear
in more than one collection, so category counts can overlap.

`collection="all"` keeps material whose collection is unknown. A specific collection
returns links with an observed classification in that collection.
`collection_counts` helps choose a collection. `count` describes the returned
slice and `total_count` the matching index after course and collection filtering. `other_courses_excluded` counts
known other courses omitted by a course filter. The index uses at most 400 links
per source page; `omitted.links` reports source-link truncation. Follow `has_more`
and `next_offset` for another slice. Exhausting these slices covers the links
indexed from those entry pages. Use `coursehub_explore` for
broader page discovery and `coursehub_page` for a specific student area. Reported
`handoffs` identify separate LMS, student-status or assessment systems. They are
not CourseHub login or write capabilities.

Use `authenticated=true` for the user's own student pages and documents. The
mathematics SSO flow uses `NINOVA_USERNAME` and `NINOVA_PASSWORD`. Chemistry also
requires `NINOVA_COURSEHUB_STUDENT_NUMBER`. The connector does not substitute a
library account number. Chemistry access requires the site's student records to
match the supplied username and student number. A mismatch stops login.

Account access is accepted only after a separate account home request shows a
logout link. The ITU SSO flow can pass its verified optional warning using
`Uyarıyı Geç »` without selecting the checkbox that suppresses future warnings.
Unknown warnings or consent forms require browser interaction. Login and reviewed
form POSTs include their native `Origin` and `Referer` context. A cross-origin
request uses an origin-only referrer to keep SSO query details private.

Authenticated page, document and material reads can refresh an already cached
session once when a trusted login destination, recognized login form or HTTP 401
establishes expiry. CourseHub signs in again and retries the same allowlisted
read. The returned `session_refreshed` flag reports a successful read after that
refresh. A newly
established session that fails does not trigger another login. HTTP 403, unknown
redirects, network errors and interrupted response bodies are reported without a
credential retry. Public reads never use account recovery.

Exploration shares one refresh allowance across the entire crawl. Its top-level
`session_refreshed` reports whether any page used that recovery, and each returned
page has its own `session_refreshed` flag. Page, document and material-index reads
also return the flag for their own request. Form review and submission use no
automatic session refresh. An unrecognized login or consent flow stops for
browser interaction.

Account pages can expose exam locations, results, materials, archives and forms.
The connector reports only what the site supplies for the signed-in user. These
areas require verification against the actual account before relying on their
coverage. Physics service errors are reported explicitly. HTTP 503 does not mean
there are no physics documents or records.

`coverage="partial"`, `errors`, `remaining_pages` and `page_limit` describe discovery
limits for `coursehub_explore`. The material index reports its entry-page scope
with `coverage="root_links_only"` or `coverage="seed_pages_only"`.
`discovered_links_exhausted` means the discovered read links were exhausted, not
that every possible account route was inspected. The hub is also useful when a department site is unavailable. External Pearson, Moodle, Ninova and video links
remain visible. Their account actions use those platforms' own interfaces.

## Reviewed account forms

CourseHub writes are disabled by default. No live write adapter is preconfigured.
`coursehub_page` exposes form IDs, editable field names, available options and
hidden-field names so an operator can inspect the site's real form in a browser.
Disabled controls and options are respected, including restrictions inherited
from a fieldset or option group. A readable page with no editable controls does
not establish an available submission. Enabling `NINOVA_COURSEHUB_WRITES=1` alone
does not authorize arbitrary form submission.

An operator must supply `NINOVA_COURSEHUB_WRITE_RULES_JSON` after verifying the
form's purpose and endpoints. A rule binds the site, page path, form ID, POST action path,
editable field names, submit button and the classification of hidden fields.
`csrf_fields` may refresh during readback. `context_fields`, such as an exam ID,
are included in the preview and must remain identical. Never classify a record
identifier as a CSRF field. If a name appears in both lists, context binding
takes precedence and its value remains immutable.

A native read-only `input` can carry unchanged context when its type is `text`,
`search`, `email`, `url`, `tel` or `number`. Its name must be explicitly listed in
`context_fields`. Its source value appears in the preview, is included unchanged
in the submission and must match when the form is fetched again. The caller
cannot supply or override it through `fields`, including when a rule mistakenly
lists the name as editable. Form metadata marks these controls with `readonly`.
Other read-only controls require a dedicated adapter.

URLs with action queries, file/password inputs, repeated names, radio buttons
and checkboxes require a dedicated adapter and are refused by the text-form
adapter.

This is a **synthetic configuration example**, not a claim that an official site
uses these endpoints:

```json
[
  {
    "site": "chemistry",
    "page_path": "/student/review.php",
    "form_id": "objection",
    "action_path": "/student/objection.php",
    "purpose": "Submit an exam objection",
    "fields": ["reason", "exam"],
    "csrf_fields": ["csrf"],
    "context_fields": ["exam_id"],
    "submit_name": "send",
    "submit_value": "yes"
  }
]
```

For a verified form, `coursehub_review_change` returns the complete destination,
fields, target context and a random action ID. Show the preview to the user.
`coursehub_commit_change(action_id=..., confirm=true)` submits that stored action once
only after approval. Previews expire after ten minutes. The form is fetched again
before POST, its structure and target context must match, and only reviewed CSRF
values may change. Changed operator rules invalidate a preview.

A transport error after submission starts, including a failure while reading the
response body, yields `status="uncertain"`. A completed HTTP 200 response yields
`submitted_unverified`. Verify the actual account state in the official site.
Consumed action IDs cannot be reused, including after an uncertain outcome.
Pending actions are not persisted or retried after a server restart.

Remote HTTP exposes the write operations only when remote writes are enabled and
`NINOVA_COURSEHUB_WRITES=1`. It retains the server's existing authentication
requirements for remote writes. No CourseHub operation accepts a host filesystem
path, submits a file, completes a Pearson quiz or changes an instructor's records.
