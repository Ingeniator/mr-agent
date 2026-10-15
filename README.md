# mr-agent: MR description and MR review bots for GitLab

LLM jobs that run in the reviewed project's own GitLab CI (`gitlab-ci.example.yml`) and work
on its open merge requests labelled `OPT_IN_LABEL` (`ai-review`): add the label to opt an MR in to both, remove it to
stop. MRs without the label are never touched.

- **`mr-description`** (`mr_description.py`) writes a summary into the MR description,
  between `<!-- ai-description:start -->` and `<!-- ai-description:end -->`.
  The block is appended on the first run and replaced after each push; text outside it is
  kept. Put the empty markers anywhere in the description to place the block there.
- **`mr-review`** (`mr_review.py`): inline comments on diff lines plus one summary note,
  checked against the root `CLAUDE.md` and the `CLAUDE.md` of every touched component
  (read from the default branch). A follow-up review covers only what changed since the
  last one.
- **`mr-review-stats`** (`mr_review_stats.py`) measures the review: which inline findings
  people acted on, which they dismissed, and how long a push waits for its review (see
  [Measuring the review](#measuring-the-review)).
- **`mr-bot-health`** (`mr_bot_health.py`) fails when the review has stopped without
  anyone noticing (see [Alerting](#alerting)).

## Where to read what

Each fact has one home; the others point to it.

| Question | Home |
|---|---|
| Why our own bot, and why polling instead of webhooks | [ADR-0001](adr/0001-own-mr-bot-on-a-schedule.md) |
| Why the bot runs in each project's CI, not as one service for all of them | [ADR-0001, "Открыто"](adr/0001-own-mr-bot-on-a-schedule.md#открыто) |
| How it works inside: incremental review, markers, fetching, retries, traces | The module docstrings: `mr_review.py`, `markers.py`, `repo.py`, `gitlab_api.for_each_open_mr()`, `telemetry.py` |
| Limits and defaults | The constants themselves; this file names them, never copies their values |
| How to set it up, run it, and read its output | This file |
| Who owns the schedules and who answers an alert | [Ownership](#ownership) |
| Changing the prompt, running the stats, rotating the token, a failed `mr-bot-health` | [Operating](#operating) |
| A picture of one run, step by step | [`overview.html`](overview.html), open it in a browser |

`tests/test_docs.py` fails when this file or `overview.html` names a constant, function or
file that no longer exists, or links to a missing file or anchor.

## Setup

The jobs run only in a **scheduled pipeline on the default branch**, never in MR
pipelines: the tokens are protected CI variables, so code from an MR branch cannot read
them. The MRs' commits are fetched and diffed, never executed.

1. Copy the jobs from `gitlab-ci.example.yml` into the project's `.gitlab-ci.yml`. Pin
   `MR_AGENT_REF` to a tag, set `LLM_BASE_URL` and `LLM_MODEL`, and describe the project in
   `MR_AGENT_PROJECT` (see [Changing the prompt](#changing-the-prompt)). The review reads
   the project's rules from its checkout: the root `CLAUDE.md` and the `CLAUDE.md` of each
   top-level directory an MR touches.
2. CI/CD → Schedules: branch `main`, variable `LLM_MR_BOT=1`, for example every 10
   minutes. "Run" on the schedule starts a run immediately.
3. CI/CD → Variables, **masked and protected**:
   - `LLM_API_KEY`: the key of the LLM endpoint.
   - `MR_BOT_TOKEN`: a project access token, scope `api`, role Developer (Reporter cannot
     edit MR descriptions). Notes and descriptions are written by its bot user.

   Do not unprotect them to run the jobs in MR pipelines: that hands the tokens to every
   branch.
4. Settings → CI/CD → Variables → "Minimum role to use pipeline variables": Maintainer.
   Otherwise a manual run on `main` with its own `CI_API_V4_URL` or `GIT_CONFIG_*` could
   send the tokens elsewhere. Not "No one allowed": the manual runs in
   [Operating](#operating) pass `MR_IID` or `REVIEW_STATS_DAYS` as pipeline variables.
5. Failure notifications and the daily `mr-bot-health` schedule: see [Alerting](#alerting).
   Without them a broken bot goes unnoticed.

## What a run does

- Goes over the open MRs, most recently updated first, and stops after `MAX_MRS_PER_RUN`
  MRs that reached the LLM. MRs with nothing new do not count, so a backlog is worked off
  over several runs. A push is picked up on the next scheduled run, not at once
  ([ADR-0001](adr/0001-own-mr-bot-on-a-schedule.md)).
- Skips an MR whose current head is already described or reviewed.
- Writes nothing if the MR head moved on while the LLM was answering: the next run handles
  the new head.
- Records a span per MR with what became of it ([Traces](#traces)).
- `resource_group` keeps a run from overlapping the next one.

The jobs keep no state of their own: hidden markers in MR descriptions and notes record
what was handled (`markers.py`). They are shared state between versions of these scripts,
so change them expand/contract.

### When something fails

- **The LLM rejects the prompt itself** (too long, content filter): that head is marked as
  failed and not retried until the next push. A description that already had a summary
  keeps it under the failure note.
- **Any other error on an MR** (network, 5xx, 429, auth, an unknown model, a commit the
  server refuses to fetch): the run turns red and the MR is retried with a growing delay,
  from `ERROR_BACKOFF` doubling up to `MAX_ERROR_BACKOFF`. Until then the MR is skipped and
  takes no slot, so an MR that always fails cannot block the others. One bot note per job
  and MR counts the failures in a row on its head (error text only in the job log). A push
  retries at once, and so does a manual run with `MR_IID`. The first run that succeeds
  rewrites the note as recovered.
- **An inline comment is rejected** (GitLab 400 on the position): the finding moves to the
  summary note. Any other posting error stops inline posting and the rest go to the
  summary.

## Measuring the review

A reviewer whose comments people learn to skip is worse than none, and that failure is
silent: the job stays green. So every inline finding carries a hidden marker with the
reviewed head, its severity, the model and a hash of the prompt templates
(`prompt_version()`), which changes by itself when the prompt does. The summary note asks
readers to rate inline comments with 👍 or 👎.

`mr-review-stats` reads back the bot's inline threads on labelled MRs updated within
`LOOKBACK` (or `REVIEW_STATS_DAYS`), in any state, and gives each finding a verdict:

| Verdict | When |
|---|---|
| useful | more 👍 than 👎; or, with no votes either way, the MR head changed the code within `NEAR_LINES` of the comment after it was posted |
| dismissed | more 👎 than 👍; or no change near the line, and the thread was resolved or the MR was merged or closed |
| pending | MR still open and nothing happened yet; or the commented head cannot be fetched any more, so a change is unknown |

**Precision** is useful / (useful + dismissed), reported overall and per prompt revision,
model and severity, next to the raw counts: resolved threads, threads with a human reply,
👍 and 👎. Findings posted before the marker existed show up as `prompt=legacy`. Column `n`
is the number of decided findings behind the precision.

Under the table, the job counts the **findings in summary notes** per severity. They have
no thread and no votes, so no verdict: this is how much the precision does not cover.

Read the numbers as a trend, not as ground truth. A nearby change may be unrelated, and a
fix may land elsewhere in the file; votes override both. A few MRs a week give a few dozen
findings a month, so look at `n` before reading anything into a difference between two
prompt revisions. Statistics deliberately left out until there is more data are listed in
the `mr_review_stats.py` docstring.

The job prints the table to its log and writes every finding with its verdict to
`review-stats.json` (a job artifact), along with the summary-note findings (`in_summary`).
Rated findings, with their diff position, are the seed
of a labelled set for replaying prompt changes offline.

Setup: a second schedule on `main` with the variable `LLM_MR_STATS=1`, weekly for example.
It uses `MR_BOT_TOKEN` only (read access is enough) and never calls the LLM.

## Traces

What each run did with each MR goes only to the OTLP collector set by
`OTEL_EXPORTER_OTLP_ENDPOINT`; without one it is in the job log and nowhere else. The spans
and the export are described in the `telemetry.py` docstring. The span of one MR,
`mr-review !<iid>` / `mr-description !<iid>`, carries `mr_bot.status`:

| `mr_bot.status` | When | Extra attributes |
|---|---|---|
| `reviewed` / `described` | the result was written to the MR | `mr_bot.inline`, `mr_bot.in_summary` (review only), `mr_bot.latency_s` |
| `skipped` | nothing new for the LLM | `mr_bot.reason`: already handled, rejected earlier, no changes, no diff refs |
| `head_moved` | a push came in while the LLM was answering; nothing written | `mr_bot.head` |
| `rejected` | the LLM rejected the prompt; retried after the next push | `mr_bot.reason`, `mr_bot.latency_s` |
| `error` | any other failure; retried with a growing delay | `mr_bot.reason`, `mr_bot.attempt` (failures in a row) |
| `backing_off` | an earlier error on this head, its retry delay has not passed | `mr_bot.attempt`, `mr_bot.reason` (next attempt) |

`mr_bot.latency_s` is the user-visible wait: from the oldest push not handled yet to the
moment the review note or description was written (on the first run on an MR, from its
current version, since earlier pushes may predate the label). MRs not reached because
`MAX_MRS_PER_RUN` was hit get no span; the run's span counts them in `mr_bot.deferred`. A
deferred MR may have had nothing new, so this is an upper bound on the backlog.

Latency percentiles, runs that hit the limit and counts per status are queries over these
spans in the trace backend.

## Alerting

The bots fail quietly. A red scheduled pipeline notifies only the schedule's owner, and
some breakages produce no red pipeline at all: a deactivated schedule, a lost `LLM_MR_BOT`
variable, rules that no longer match, a pipeline stuck pending without a runner. Two
safeguards cover these cases:

1. **Failure notifications for the team.** Settings → Integrations → *Pipeline status
   emails*: the team's addresses as recipients, *Notify only broken pipelines* on,
   branches *Default branch*. The *Slack notifications* or *Mattermost notifications*
   integration with the *Pipeline* event does the same for a chat channel.
2. **`mr-bot-health`, a daily check that something actually got reviewed.** It fails when:
   - an open labelled MR has had its current head unreviewed for longer than
     `STALE_AFTER`. An MR backing off after errors counts as unreviewed, because that is
     the case where nobody else may be watching;
   - `MR_BOT_TOKEN` expires within `TOKEN_WARN_DAYS`. Only a Maintainer can rotate a
     project access token, and an expired one stops both bots at once;
   - the token no longer works at all (401), or an MR cannot be read.

   The log lists every labelled MR with how long its head has waited, and ends with one
   `PROBLEM:` line per failure.

   Setup: a third schedule on `main` with the variable `LLM_MR_HEALTH=1`, daily. It uses
   `MR_BOT_TOKEN` only and never calls the LLM. Give it **a different owner** from the
   `LLM_MR_BOT=1` schedule: a schedule runs as its owner, so with one owner, one person
   losing access stops the bots and their check together ([Ownership](#ownership)).

   What to do when it fails: [When `mr-bot-health` fails](#when-mr-bot-health-fails).

## Ownership

The bots need two maintainers, so that one person leaving, being away or losing access
stops nothing. Both have the Maintainer role in the project: only a Maintainer can rotate
`MR_BOT_TOKEN`, take over a schedule, or pass pipeline variables (Setup, step 4).

| Role | Who | Where GitLab shows the current holder |
|---|---|---|
| Owner of the `LLM_MR_BOT=1` and `LLM_MR_STATS=1` schedules; changes the prompt, reads the weekly stats | _first maintainer: to fill in_ | CI/CD → Schedules, column *Owner* |
| Owner of the `LLM_MR_HEALTH=1` schedule; first to answer a failed `mr-bot-health` | _second maintainer: to fill in_ | CI/CD → Schedules, column *Owner* |
| Receive failed scheduled pipelines | both maintainers and the team channel | Settings → Integrations → *Pipeline status emails* (and *Slack* / *Mattermost notifications*) |

A schedule runs as its owner. When the owner loses access or the Maintainer role, GitLab
deactivates the schedule or its pipelines fail, so the two schedules have different owners:
the health check then outlives the person whose schedule stopped, and fails the next day.
When someone hands over a role, the new holder opens the schedule, *Take ownership*, and
updates the table above.

Nothing checks the health schedule itself. Whoever reads the weekly stats also looks at
CI/CD → Schedules: all three schedules *Active*, each with a recent *Last pipeline*.

**Handover is done** when the second maintainer, without asking the first:

- has the Maintainer role, owns the `LLM_MR_HEALTH=1` schedule and gets the failure emails;
- has run `mr_review.py` as a dry run against one MR ([Running locally](#running-locally));
- has run the stats once and read the table ([Running the stats](#running-the-stats));
- has done the next rotation of `MR_BOT_TOKEN` ([Rotating `MR_BOT_TOKEN`](#rotating-mr_bot_token)),
  the first maintainer only watching.

Whatever the second maintainer had to ask along the way goes into this file.

## Operating

### Changing the prompt

The prompt text is in three places: `PROMPT` and `FOLLOW_UP` (added to follow-up
reviews) in `mr_review.py`, `PROMPT` in `mr_description.py`, and `PROJECT`, shared by
both: the project's own text, taken from `MR_AGENT_PROJECT` in its CI (`project.py`). The review also sends the rules (the root `CLAUDE.md` and
those of the touched components, cut at `MAX_RULES_CHARS`) and the diff (cut at
`MAX_PROMPT_DIFF_CHARS`).

1. Edit the template. The templates go through `str.format`: `{name}` is a placeholder, a
   literal brace is doubled (`{{`). The JSON answer that `PROMPT` in `mr_review.py` asks
   for is read by `parse_review()`; change both together, or every review fails.
2. Try it on real MRs from your branch before merging, as a dry run
   ([Running locally](#running-locally)) on two or three MRs of different size, and
   compare with what the bot posted there:
   - review: leave `MR_BOT_USER_ID` unset. The run then does not see the bot's earlier
     review and reviews the whole MR with the new prompt;
   - description: a head that already has a description is skipped whoever the bot is,
     so pick an MR whose description has no `ai-description` markers. `MR_IID` takes MRs
     without the label too.
3. Run the [tests](#tests) and merge to `main`. The next scheduled run uses the new
   prompt, and only on new pushes: heads already reviewed or described are not redone.
4. Write the new `prompt_version()` into the MR description so the stats rows can be traced
   to the change:

   ```sh
   MR_AGENT_PROJECT="<the project's value>" python3 -c 'import mr_review; print(mr_review.prompt_version())'
   ```

   Run it in the mr-agent checkout. It covers the review templates, `PROJECT` included. It does not cover the `CLAUDE.md`
   rules or `LLM_MODEL` (the stats show the model separately), and the description
   prompt has no version at all.
5. A few weeks later, compare the new revision's precision with the previous one in the
   stats ([Measuring the review](#measuring-the-review)), and look at `n` first.

### Running the stats

- **The usual report:** CI/CD → Schedules → the `LLM_MR_STATS=1` schedule → *Run*. When
  the pipeline finishes, the `mr-review-stats` job log has the table, and *Download
  artifacts* has `review-stats.json`. How to read it:
  [Measuring the review](#measuring-the-review).
- **A different window:** CI/CD → Pipelines → *Run pipeline* on `main` with the variables
  `LLM_MR_STATS=1` and `REVIEW_STATS_DAYS=<days>` (Maintainer, Setup step 4).
- **On your machine:** [Running locally](#running-locally), with `MR_BOT_USER_ID` set,
  or it finds none of the bot's findings.

The job only reads. A run any day is fine.

### Rotating `MR_BOT_TOKEN`

`mr-bot-health` names the expiry date in every log and fails `TOKEN_WARN_DAYS` days
ahead. A Maintainer rotates the token:

**Rotate it, do not create a new one.** A new project access token comes with a new bot
user, and the jobs recognise their notes by author (`bot_user_id()`). After a new token
every labelled MR is reviewed again from scratch, the old error notes are ignored, and the
stats lose every earlier finding. Rotation keeps the bot user.

1. Find the token's id: Settings → Access tokens lists the project's tokens; the API gives
   the id:

   ```sh
   curl -H "PRIVATE-TOKEN: <your personal token, scope api>" \
     "https://<gitlab host>/api/v4/projects/<project id>/access_tokens"
   ```

2. Rotate it, **with an expiry date**. Without `expires_at` GitLab sets a short default,
   and the health check fails again soon:

   ```sh
   curl -X POST -H "PRIVATE-TOKEN: <your personal token, scope api>" \
     "https://<gitlab host>/api/v4/projects/<project id>/access_tokens/<token id>/rotate?expires_at=<YYYY-MM-DD>"
   ```

   The answer's `token` is the new value. The old one stops working at once, so a job
   running now may fail; the next run retries.
3. CI/CD → Variables → `MR_BOT_TOKEN` → paste the new value; keep it masked and protected.
4. Run the `LLM_MR_HEALTH=1` schedule: the log shows the new expiry date and ends with
   `The MR bots are healthy.`

**If the token has already expired or been revoked**, it cannot be rotated. Create a new
one as in Setup step 3, put it into `MR_BOT_TOKEN`, and expect the costs above: a full
review of every labelled MR within a few runs (with the run limit, a backlog), and stats
that start from zero.

### When `mr-bot-health` fails

Open the failed job's log. Its last lines are one `PROBLEM:` line per failure; handle each
one below. A log that ends with a traceback and no `PROBLEM:` line is the last case.

**`MR_BOT_TOKEN expires on …`.** Nothing is broken yet: [rotate it](#rotating-mr_bot_token).

**`… labelled MR(s) unreviewed for over … h: !<iid>, …`.** Go down the list and stop at
the first step that explains it:

1. CI/CD → Schedules: is the `LLM_MR_BOT=1` schedule *Active*, with a recent *Last
   pipeline*? If not, its owner has lost access or someone turned it off: a Maintainer
   opens it, *Take ownership*, checks the variable `LLM_MR_BOT` is still `1`, activates
   it and presses *Run* ([Ownership](#ownership)).
2. The last pipeline ran but has no `mr-review` job: the job's `rules` in `.gitlab-ci.yml`
   no longer match. Look at the recent changes to that file.
3. The pipeline is pending or the job hangs: runners, not the bot (Settings → CI/CD →
   Runners). A hung job holds its `resource_group` and every later run waits behind it;
   cancel it.
4. `mr-review` is red: in its log, find `=== !<iid>` and the line under it,
   `!<iid> failed: <error>`.
   - The `LLM call:` line above it ends with an HTTP status: 401 is `LLM_API_KEY`,
     429 or 5xx is the LLM gateway, 404 or an unknown model is `LLM_MODEL` in
     `.gitlab-ci.yml`.
   - `HTTPError` 401 or 403 from GitLab: `MR_BOT_TOKEN` (see the last case below).
   - A failed fetch of a commit, the same MR every time: the problem is that MR. See
     [when to remove the label](#when-to-remove-the-label).

   The MR also carries a bot note counting the failures in a row. After the fix, a push to
   the MR retries it at once, and so does CI/CD → Pipelines → *Run pipeline* on `main` with
   `LLM_MR_BOT=1` and `MR_IID=<iid>`. Otherwise the retry waits for its delay.
5. `mr-review` is green, yet the MR waits:
   - `Failed … time(s) on …, next attempt after …` under `=== !<iid>`: the MR is
     waiting out the retry delay after earlier failures. Go back to step 4 with the
     failed job's log.
   - `Run limit of … MRs reached` in several runs in a row: more labelled MRs have
     changes than the schedule works off. It clears on its own; if it keeps happening,
     run the schedule more often.
   - `MR head moved on during the review` each time: the MR is pushed faster than it is
     reviewed. It clears when the pushes stop.
   - Every labelled MR waits at once, just after a token change: a new token, new bot
     user (see [Rotating `MR_BOT_TOKEN`](#rotating-mr_bot_token)). It clears as the backlog
     is reviewed.

**`!<iid> could not be checked: …`.** The error is in the line. A deleted MR or a lost
permission clears by itself on the next run; *Run* the health schedule to see. If it stays,
treat it as a problem of that MR.

**A traceback with `HTTP Error 401` and no `PROBLEM:` line.** `MR_BOT_TOKEN` has expired
or been revoked, and both bots have stopped: create a new one
([Rotating `MR_BOT_TOKEN`](#rotating-mr_bot_token), last paragraph). Any other traceback,
usually the network to GitLab: *Run* the health schedule again, and look further if it
fails twice.

#### When to remove the label

Remove `ai-review` from an MR when the cause is in that one MR and will not be fixed
today: its commits cannot be fetched, or it fails the same way on every run. Say why in a
comment on the MR. Without the label the MR gets neither a review nor a description, and
the health check stops counting it. Put the label back once it is fixed: the next run picks
the MR up.

Do not remove labels when the cause is global (token, schedule, runners, LLM gateway).
Every MR would lose its opt-in, and the alert would go quiet while nothing works. Fix the
cause. If that takes days, say so in the team channel and let the check stay red: the red
job is the reminder. Never deactivate the `LLM_MR_HEALTH=1` schedule.

## Environment

| Variable | Where it comes from | Meaning |
|---|---|---|
| `LLM_BASE_URL`, `LLM_MODEL` | `.gitlab-ci.yml` | OpenAI-compatible endpoint and model id, sent as is (no litellm prefix). |
| `LLM_API_KEY` | protected CI variable | LLM key. |
| `MR_AGENT_PROJECT` | `.gitlab-ci.yml`, optional | What the prompts say about the project (`project.py`). Unset: nothing. |
| `MR_BOT_TOKEN` | protected CI variable | GitLab token the bot acts with. |
| `CI_API_V4_URL`, `CI_PROJECT_ID` | predefined by GitLab | Project API. |
| `DRY_RUN=1` | manual run only | Print every write instead of sending it. Nothing is marked as done, so do not leave it on the schedule: every run would send the same MRs to the LLM again. |
| `MR_IID` | manual run only | Handle only this MR, labelled or not. |
| `REVIEW_STATS_DAYS` | `mr-review-stats`, optional | How far back to look at updated MRs; `LOOKBACK` by default. |
| `REVIEW_STATS_JSON` | `mr-review-stats`, optional | Where to write the report; `OUTPUT` by default. |
| `MR_BOT_USER_ID` | local runs | The bot user's id, when `MR_BOT_TOKEN` is not the bot's own token (see [Running locally](#running-locally)). |
| `GIT_REMOTE` | local runs | Git remote (name or URL) to fetch MR commits from; `origin` by default. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | CI variable, optional | OTLP/HTTP collector base URL; spans go to `<url>/v1/traces`. Unset: no export. Spans and attributes: the `telemetry.py` docstring. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | CI variable, optional | Full traces URL, used as is; wins over the base URL. |
| `OTEL_EXPORTER_OTLP_HEADERS` | masked CI variable, optional | Collector auth, `key=value,key2=value2` (values URL-encoded), as in the OpenTelemetry spec. |
| `OTEL_SERVICE_NAME` | CI variable, optional | `service.name` of the spans; the job name (`CI_JOB_NAME`) by default. |

## Running locally

A dry run against one MR needs only read access to GitLab: a personal token with
`read_api` is enough. From the root of a clone of the reviewed project, with the GitLab
project as `GIT_REMOTE` when your `origin` is elsewhere:

```sh
CI_API_V4_URL=https://<gitlab host>/api/v4 CI_PROJECT_ID=<project id> \
MR_BOT_TOKEN=<personal read_api token> LLM_API_KEY=<llm key> \
LLM_BASE_URL=https://<llm gateway>/v1 LLM_MODEL=gpt-4o \
GIT_REMOTE=<gitlab clone url> DRY_RUN=1 MR_IID=<iid> \
python3 <mr-agent checkout>/mr_review.py   # or mr_description.py
```

The review is printed as `[dry run] POST …` lines.

The jobs tell the bot's notes from others by their author: by default, the user the token
belongs to. With a personal token that is you, so add `MR_BOT_USER_ID=<bot user id>` (from
the bot user's profile page). Without it the review does not see the bot's earlier review
and reviews the whole MR again, and the stats find none of the bot's findings.

`mr_review_stats.py` takes the same `CI_API_V4_URL`, `CI_PROJECT_ID`, `MR_BOT_TOKEN` and
`GIT_REMOTE`, and neither the LLM variables nor `DRY_RUN`: it only reads. So does
`mr_bot_health.py`, without `GIT_REMOTE`.

Every LLM call, failed ones included, prints one line to the job log (`llm.py`):

```
LLM call: 41.3s, prompt 148213 chars, 41020 prompt + 812 completion tokens, finish_reason stop
```

The tokens give the cost, the prompt size shows how close the MRs come to the model's
context limit (`LLMRejected`), and the time is the request alone. A failed call ends with
the HTTP status or the error type instead of `finish_reason`. The prompt itself is never
logged.

## Code

Standard library only: the jobs run on the CI image's bare `python3`.

| File | What |
|---|---|
| `mr_description.py`, `mr_review.py` | The two jobs: prompt, decision what to send, what to write back. |
| `mr_review_stats.py` | What became of the review's inline findings: verdicts and precision. |
| `mr_bot_health.py` | Daily check: labelled MRs left unreviewed, `MR_BOT_TOKEN` close to expiry. |
| `gitlab_api.py` | GitLab REST client (`MergeRequest`, dry run and quick-action escaping on every write) and the loop over open MRs. |
| `llm.py` | One chat completion; tells prompt rejections (`LLMRejected`) from other errors and logs each call. |
| `diff.py` | Unified diff parsing: line numbers for the prompt, commentable lines for GitLab. |
| `repo.py` | Git commands on the job's clone, fetching MR commits by SHA. |
| `markers.py` | The hidden markers: the jobs' only state between runs. |
| `project.py` | `PROJECT`: everything the prompts say about the reviewed project, from `MR_AGENT_PROJECT`. |
| `common.py` | Environment and HTTP helpers. |
| `telemetry.py` | Spans of a run, exported as OTLP/HTTP JSON when a collector is configured. |
| `gitlab-ci.example.yml` | The jobs to copy into the project's `.gitlab-ci.yml`. |
| `tests/` | Tests against a throwaway git server shaped like GitLab and a fake GitLab API; `test_docs.py` checks the docs against the code. |

## Tests

```sh
python3 -m unittest discover -b -s tests -t .
basedpyright --pythonversion 3.11 --warnings *.py && basedpyright --pythonversion 3.11 --level error tests/*.py
```
