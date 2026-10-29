# Readwise News Digest

A small program for macOS that reads the news feeds you already follow in
[Readwise Reader](https://readwise.io/read), picks the 25 articles most worth your
time each day, and tags them `newsdigest`. There is no app or web page to open:
you read the picks in Reader, where you already read, and Reader stays the only
screen.

It also learns from what you do (highlight, shortlist, finish, archive), writes you
a reading report every month, and now and then suggests sites worth following.

## Contents

- [What it does, and why](#what-it-does-and-why)
- [What you need](#what-you-need)
- [Install](#install)
- [Use it in Reader](#use-it-in-reader)
- [How the picks are made](#how-the-picks-are-made)
- [Reading reports and feed suggestions](#reading-reports-and-feed-suggestions)
- [Settings](#settings)
- [What leaves your Mac](#what-leaves-your-mac)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Limits](#limits)
- [For developers](#for-developers)

## What it does, and why

**The problem.** The Feed in Reader fills with hundreds of items a day. Most people
either skim headlines and archive the lot, or stop looking. The articles worth an
hour are in there, buried.

**What this does about it.** Once a day it looks at the new items from the feeds
you chose, asks an AI model (Anthropic's Claude Haiku) how well each one fits your
interests and how substantial it is, and tags the best 25 with `newsdigest`. Three
more are picked at random from just below the cut, so it keeps learning about
things it would otherwise never show you. You open a saved view in Reader that
shows `tag:newsdigest`, and that is your digest.

**It learns from what you do.** Every hour it looks at what you did with the
tagged articles: highlighted something, put your own tag on it, shortlisted it,
read most of it, archived it unopened, or ignored it for a month. Each article gets
a grade from 1 to 5 from that behaviour, and the grades nudge future scores up or
down for topics, sites, and styles of writing. You never have to rate anything, but
you can: tag a pick 👍 or 👎 (see [Voting](#voting-with--and-)).

**It keeps you informed about your own habits.** At the start of each month it
adds a note to Reader with a report on your reading: which sources you used most,
a split by topic and by part of the world, how the digest's picks did, and when you
read. In January you also get a report for the whole year. About every second
month the report also lists a few sites you keep saving articles from but don't
follow yet.

**What it changes in Reader.** Only two things: it adds tags (`newsdigest` on the
picks, `newsdigest-copy` on a saved copy of a pick; your own tags stay) and it
saves its own report notes to your Later list. It never deletes, moves or archives
anything of yours.

Here is the top of a monthly report (the numbers are made up):

> **March 2027 at a glance**
> - You touched 212 articles from 64 sources: 190 opened, 61 read at least halfway
>   (29%), 18 of those long reads (over 1500 words).
> - You saved 40 articles yourself and highlighted 77 passages in 19 articles.
> - Reading time, counting the part of each article you got through: about 9 hours.
>
> **Sources you used most** (a table of the top ten), **What you read about**,
> **Where in the world**, **How the digest did**, **When you read**, **Most
> highlighted**.

## What you need

- **A Mac** running a recent macOS. It relies on launchd (the Mac's scheduler), the
  Keychain (where your keys are stored) and macOS notifications. It was built and
  tested on macOS Tahoe (26). Other systems are not supported.
- **Python 3.10 or newer** (for example `brew install python`) and the **Xcode
  Command Line Tools** (`xcode-select --install`), which provide the small compiler
  used for the launchers.
- **A Readwise Reader account** with some feeds in it, and your Readwise access
  token from <https://readwise.io/access_token>.
- **An Anthropic API key** from <https://console.anthropic.com>. This is pay as you
  go and separate from any Claude subscription. The program makes one Haiku call
  per 50 new feed items plus a handful a month for reports, which is typically cents
  a day. Set a spending limit in the Anthropic console and watch your first week of
  usage there.
- **Your Mac awake some of the time.** It doesn't wake your Mac at night. If the
  Mac was asleep when a job was due, the job runs when the Mac wakes.

## Install

Each step is one copy-and-paste. Run them in Terminal.

**1. Download it.**

```bash
git clone https://github.com/kerim/readwise-news-digest.git
```

```bash
cd readwise-news-digest
```

**2. Set it up.** This creates a private Python environment, compiles two tiny
launcher programs and writes the two scheduler job files for wherever you put the
folder. It asks for no passwords and loads nothing yet.

```bash
./install.sh
```

**3. Store your two keys.** Each command asks you to paste a secret and stores it in
the macOS Keychain; nothing is written to a file. Paste your Readwise token for the
first, your Anthropic key for the second.

```bash
security add-generic-password -a readwise-news-digest -s readwise-news-digest-token -U -w
```

```bash
security add-generic-password -a newsdigest -s newsdigest-anthropic-api-key -U -w
```

**4. Make a feed folder in Reader.** Create a folder called exactly
`News Digest feeds` and put in it the feeds you want in the digest (a feed can also
be in other folders). In Reader on the web, open
[Manage feeds](https://read.readwise.io/feed/sources) and use the *Manage folders*
dropdown on each feed; on mobile, use the Feed sidebar. Only feeds in this folder
are scored, so you can keep casual feeds out of the digest.

**5. Export your feed list.** Reader's API can't say which folder a feed is in, so
the program reads a list you export. Open your
[profile page](https://read.readwise.io/profile), and under *Export account data*
choose *Export Feeds as OPML*. It lands in your Downloads folder as
`Reader_Feeds.opml`. The program always uses the newest `Reader_Feeds*.opml` it
finds there. **Export again whenever you add or remove a feed in that folder.**

**6. Describe your interests.** The scorer needs to know what you care about. Copy
the example and edit it:

```bash
cp interest-profile.example.json interest-profile.json
```

Open `interest-profile.json` in any text editor. It is a list of topics, each a
short plain-English name, most important first:

```json
{
  "topics": [
    {"topic": "Climate policy and the energy transition", "tags": ["climate", "energy"], "score": 1.0},
    {"topic": "Artificial intelligence and its social effects", "tags": ["ai"], "score": 0.8}
  ]
}
```

`topic` is what the AI matches articles against, so make it a phrase it can
recognise (2 to 8 words). `score` runs from 0 to 1 and says how much you care.
`tags` are optional hints. Eight to twenty topics works well. (If you keep your
Readwise highlights in an Obsidian vault, the program can write this file for you;
see [Settings](#settings).)

**7. Check everything.** This asks Readwise and Anthropic whether your keys work,
looks for your feed list and profile, and tells you in plain words what is missing.
It also keeps a copy of your exported feed list in this folder, so the background
jobs don't depend on reading your Downloads folder. (The first time, macOS may ask
whether Terminal may access Downloads; say yes.)

```bash
.venv/bin/python check_setup.py
```

**8. Try it without changing anything.** This scores today's new items and prints
the 28 picks. It writes nothing to Reader (it does call Anthropic, so it costs a
little).

```bash
.venv/bin/python run.py --dry-run
```

**9. Turn it on.**

```bash
./reload-launchers.sh
```

That loads the two background jobs, and the hourly one runs straight away. If it is
after 05:00 that first run tags today's picks; otherwise the picks are tagged by the
first hourly run after 05:00. About a minute later you can confirm it ran:

```bash
tail -3 run.log
```

(Each run adds a line like `2027-03-02 07:07 tagged=28 scored=311 graded=0
learned=0.0`.) The jobs also show up as `readwise-news-digest-run` and
`readwise-news-digest-profile` in System Settings → General → Login Items &
Extensions. Problems appear as macOS notifications from **Readwise News Digest**
(a tiny app `./install.sh` builds in `bin/`, so they have a name of their own). By
default macOS shows them as banners that vanish after a few seconds; to keep them
on screen until you close them, go to System Settings → Notifications → Readwise
News Digest and choose **Persistent**. Missed ones stay in Notification Center.

**It tells you when it has quietly stopped working,** not only when something
fails. The two jobs check on each other through `run.log`, and you get a
notification (at most once a day each) when:

- the hourly job has not run since the daily job last did, or the daily job has not
  run today although the hourly job has run several times since 03:00;
- no articles have been picked on two days the Mac was on;
- no article's grade has changed for 14 days;
- articles are old enough to learn from but the learning produces nothing.

Each monthly report ends with a line on how many days each job ran and which
problems were reported.

## Use it in Reader

**Make the digest view.** In Reader on the web, open
[Manage views](https://read.readwise.io/views) (or press `Shift+F`), choose *Add
filtered view*, type the query below, click *Filter by query*, then *Save view* and
give it a name. Pin it to your sidebar or Home page if you like. On mobile: Search
tab, the `+` icon, the same query, then Save. That view is your digest.

```
tag:newsdigest AND feed:true AND saved__after:"24 hours ago" AND seen:false
```

It shows today's picks that you haven't opened yet: `feed:true` keeps it to feed
items, `saved__after:"24 hours ago"` to the latest day's picks, and `seen:false`
drops each one once you open it. To come back to a pick later, shortlist it or move
it to Later. The plain query `tag:newsdigest` shows every pick ever made, plus the
monthly report notes, which this view leaves out because they are saved to Later,
not your feed.

**Just read as you normally would.** What you do with a pick is the main feedback
(you can also vote; see below):

| What you did | Grade |
|---|---|
| Tagged it 👍, highlighted it, or put a tag of your own on it | 5 |
| Read at least half of a long article (1,500+ words) | 4 |
| Shortlisted it, or read at least half of a medium article (500–1,499 words) | 3 |
| Opened a longer article, read less than half, then archived it; or left it in Later unopened for 21 days | 2 |
| Left it unopened in Feed for 30 days | 1.5 |
| Archived it without opening it | 1 |
| Anything else (including short pieces you opened) | no grade |

Two more rules: keeping a saved full-length copy is worth at least a 3, and at the
90-day mark a longer article that is still sitting in Later or Feed and was opened
but not finished gets a 2.

### Voting with 👍 and 👎

Add the tag **👍** or **👎** to a pick (or to your saved copy of it). Both are
strong signals, and neither counts as "a tag of your own".

- **👍** grades the article 5, counts double in the learning, and counts from the
  day you add it (other articles wait a month, until "left alone" is known). Each 👍
  also gives its topic, site, type of article and angle a direct boost from the next
  daily pick, fading by half each month like everything else.
- **👎** counts only with a reason. Write it in the article's **document note**
  ("An event announcement, not an article"). The scoring AI reads your noted 👎s
  (and noted 👍s) at every daily pick, as rules from you for similar articles. A 👎
  without a note changes nothing: the program never guesses why. The 👎 itself
  never lowers the site or topic, since your reason is usually about something
  narrower; the article is still graded by what you did with it, as usual.

A vote or note is picked up at the next day's picks at the latest (the first run of
each day re-reads every pick).

A few consequences worth knowing:

- **Highlighting is the strongest signal.** If something was good, highlight a
  line.
- **Archiving the whole Feed at once** grades every unopened pick as a 1 and teaches
  it that you didn't want them. Archive deliberately, or pick through first.
- **Paywalled and teaser feeds.** Some feeds deliver only a few lines. If you open
  the full article in your browser and save it to Reader, the program notices the
  saved copy (same address, same shared id, or same headline), tags it
  `newsdigest-copy`, and grades the pair using the copy's real length and your
  reading of it. Without this, a paywalled source could only ever look "short" and
  collect low grades.
- Tracking stops 90 days after tagging. Deleting a pick, or removing its
  `newsdigest` tag, counts as archiving it.

To see every pick's grade and why:

```bash
.venv/bin/python run.py --grades
```

**Adding or removing a digest feed:** change the `News Digest feeds` folder in
Reader, then export the OPML again (step 5). Until you do, the program doesn't
know.

**Following a suggested site:** the report lists each site with a feed address that
has been checked. In Reader add that feed; put it in `News Digest feeds` if you
want it in the digest, then export the OPML again.

**Silencing things you don't want:** see `muted_sources`, `form_mutes` and
`frame_mutes` under [Settings](#settings).

**Pausing it:** run `./uninstall.sh`, or `launchctl bootout gui/$(id -u)/com.readwisenewsdigest.run`
to stop just the hourly job. `./reload-launchers.sh` turns it back on.

## How the picks are made

1. **Candidates.** New items from feeds in your `News Digest feeds` folder that it
   hasn't scored before. The same article arriving twice is counted once.
2. **Scoring.** Claude Haiku gives each item two scores from 1 to 10: *topic fit*
   against your profile (the first 20 topics in it are used) and *form fit*. Form fit
   is a built-in taste for substantial writing: analysis, essays, reporting and
   criticism score high; wire copy, breaking-news briefs and press-release rewrites
   score low. It uses the headline, the start of the summary, the publication and URL
   clues (a `/longread/` path counts for, a `/live/` path against). An item's score
   is the lower of its two. Items below a junk floor are dropped, and videos and
   podcasts are skipped.
3. **Choosing.** The top 25 by score, with at most three per topic and three per
   site (relaxed when there are too few topics or sites to fill the list), plus three
   picked at random from ranks 26 to 60.
4. **Tagging.** `newsdigest` is added to those 28 in Reader, keeping any tags they
   already had.
5. **Learning.** Every hour the grades above are updated, and they nudge future
   scores by topic, site, form and angle, always measured against the average so
   everything can't sink together. An article counts once it is a month old (a 👍
   at once); your Readwise profile sets the starting scores, so there is no minimum
   to reach first.

If you prefer a different taste, the scoring prompt is in `scoring.py`, and the
sizes and thresholds are in the [tuning table](#tuning-constants) below. Everything
the program remembers is in JSON files in this folder (`tracked.json` is the record
of what it tagged); delete them to start over.

## Reading reports and feed suggestions

The daily 03:00 job decides what is due.

**Monthly report.** On the first run of a new month it saves a note to Reader's
Later list titled **News Digest Monthly Report — <Month Year>**, tagged `newsdigest`
and `newsdigest-report`. It covers everything in your Reader library, not only the
digest: an article counts for a month if you opened it, saved it yourself or
highlighted it in that month (a feed item and its saved full-length copy count
once). It contains:

- the month in numbers: articles touched, opened, read at least halfway, long
  reads, saved, highlights, and about how many hours of reading;
- the sources you used most, as a table of the top ten;
- a split by topic and by part of the world, labelled by Claude Haiku from each
  headline and summary, so take it as a rough picture;
- how the digest's own picks did;
- when you read, and your most highlighted articles.

A month that isn't over is only ever previewed. If something fails, nothing is
recorded and the next day's run tries again. The first time the daily job runs after
you install, it delivers a report for the previous month (if you read anything then),
and the suggestions section if you have saved articles to base it on.

**Annual report.** In January, a note titled **News Digest Annual Report — <Year>**
adds up the year from the monthly numbers it kept. It says so when it has fewer than
twelve months to add up.

**Feed suggestions.** About every second monthly report has an extra section, *Sites
to consider following*: sites you saved at least three articles from in the last six
months that match no feed in your OPML, ranked by how many you saved, highlighted
and read at length. At most five, each with a feed address that was fetched and
checked, and no site is ever suggested twice. This part uses no AI and no web
search; the evidence is only your own saving.

You can look at a report without delivering it:

```bash
.venv/bin/python report.py --month 2026-10 --dry-run --html october.html
```

(Use any month that has already ended. That prints the report and also draws it into
`october.html` to open in a browser. It does ask Anthropic to label the topics, and
remembers the answers.)

## Settings

Everything works with no settings file. To change anything, create `prefs.json` in
this folder. `prefs.example.json` shows the shape; copy it, then keep only the keys
you want. Every key is optional.

| Key | What it does |
|---|---|
| `muted_sources` | Sites (host without `www.`) to never pick, for example `["example.com"]` |
| `form_mutes` | Forms to hide, per topic or for all topics with `"*"`: `{"*": ["recap"], "Some topic": ["news"]}`. Forms: criticism, analysis, essay, investigation, interview, memoir, op-ed, recap, news, explainer |
| `frame_mutes` | Angle labels to hide (the short phrase the AI gives each article, shown by `--dry-run`) |
| `source_tiers` | Tell the scorer what kind of publication a site is, beyond its short built-in list: `{"example.org": "weekly_longform"}`. Kinds: `weekly_longform`, `magazine`, `daily_mixed`, `wire` |
| `report_topics` | Your own list of topics for the report's split, replacing the built-in thirteen |
| `report_regions` | Your own list of regions for the report's split, replacing the built-in twelve (for example to give one country its own line) |
| `vault_dir` | Optional. An Obsidian vault in which the Readwise plugin keeps your highlights, in its usual layout (`Readwise/Articles`, `Readwise/Books`, `Readwise/Tweets`) and with at least 100 notes. If set, the program **rebuilds `interest-profile.json` from it every week, replacing what is there**, and mixes in what you engaged with. It uses `qmd` too if you have it installed with a collection called `readwise`. Without this key your hand-written profile is used as it is |
| `vault_topic_prefix` | Optional, with `vault_dir`. Which of your highlight tags are topics: with `">>"` only tags that start with `>>` count; left out, every tag counts |

For example, to give one country its own line in the report and to use a highlights
vault whose topic tags start with `>>`:

```json
{
  "report_regions": ["United States", "Europe", "Iceland", "Global or no single place"],
  "vault_dir": "~/Documents/Obsidian/Readwise Highlights",
  "vault_topic_prefix": ">>"
}
```

### Tuning constants

These are plain constants in the code if you want to change the behaviour.

| What | Value | File |
|---|---|---|
| Picks per day / near-misses | 25 / 3 | `selection.py` (`DIGEST_SIZE`, `NEAR_MISS_COUNT`) |
| Near-miss pool (ranks of the above-junk list) | 26–60 | `selection.py` |
| Diversity caps (per topic / per site) | 3 / 3 | `selection.py` |
| Junk floor (raw score below this is dropped) | 3 | `scoring.py` (`JUNK_FLOOR`) |
| Items per Haiku call | 50 | `scoring.py` (`BATCH_SIZE`) |
| Model | Haiku 4.5 | `common.py` (`MODEL`) |
| First tagging run of the day | 05:00 | `run.py` (`TAG_AFTER_HOUR`) |
| Long / medium article | 1,500 / 500 words | `grading.py` |
| "Read" means at least | 50% | `grading.py` (`READ_FRACTION`) |
| Unopened in Later before a 2 / in Feed before a 1.5 | 21 / 30 days | `grading.py` |
| Tracking stops | 90 days after tagging | `grading.py` (`FREEZE_DAYS`) |
| Learning: items counted / half-life | 30–120 days old (👍: 0–120) / 30 days | `learning.py` |
| 👍: weight / direct boost per fresh 👍 | 2× / 0.3 per dimension | `learning.py` (`THUMBS_UP_WEIGHT`, `THUMBS_UP_BOOST`) |
| Noted 👍/👎 shown to the scorer | newest 30 | `scoring.py` (`FEEDBACK_LIMIT`) |
| The digest folder in Reader | `News Digest feeds` | `feeds_membership.py` (`DIGEST_FOLDER`) |
| Suggestions: look-back / saves a site needs / sites per report | 180 days / 3 / 5 | `suggest.py` |
| Suggestions come every | 55 days (carried by a monthly report) | `suggest.py` |
| Report: sources in the table / headlines labelled per call | 10 (annual 15) / 50 | `report.py` |
| Hourly job minute / daily job time | :07 / 03:00 | `launchd/*.plist.template`, then `./install.sh` and `./reload-launchers.sh` again |

## What leaves your Mac

- **To Readwise:** your access token (to read your library and add tags/notes). The
  program talks only to `readwise.io`.
- **To Anthropic:** for scoring, the title, site, the start of the summary and the
  URL clues of each new feed item from your digest folder, together with the topics
  in your interest profile. For the report, the headline, site and the start of the
  summary of each article you touched that month. If you use a highlights vault, the
  profile step also sends tag names, a few short snippets from your highlights and
  the titles of picks you graded 4–5 or 1. Your Anthropic account's own data
  policy applies to what you send.
- **To feed sites:** when it looks for a feed for a suggested site, and when it works
  out which sites a feed links to, it requests those public web pages the way any
  feed reader would.
- **Nowhere else.** No analytics, no accounts, no server of its own.
- **Stored on your Mac:** the two keys in the Keychain; everything else in plain
  JSON files in this folder (your tagging record, grades, report numbers, a cache of
  topic labels, a copy of your feed list). They are listed in `.gitignore` so they
  are never committed if you fork this.

## Troubleshooting

Run `.venv/bin/python check_setup.py` first; it names the problem for most cases.

- **A notification said something is wrong.** Problems are logged and shown once a
  day each. The log lines are in `readwise-news-digest.log`, and each run adds one
  line to `run.log`.
- **"No feed list yet".** Do step 5 (export the OPML) and make sure the file is in
  your Downloads folder.
- **Nothing was tagged.** Check the folder is called exactly `News Digest feeds`,
  that you exported the OPML after putting feeds in it, and that the Mac was awake
  after 05:00. `run.py --dry-run` shows what it would pick.
- **`./reload-launchers.sh` fails with "Operation not permitted" or an input/output
  error.** Give your Terminal app *App Management* permission in System Settings →
  Privacy & Security, then run it again. If that isn't possible, log out and back
  in; macOS loads everything in `~/Library/LaunchAgents` at login.
- **The picks look wrong.** Improve `interest-profile.json` first (clearer, more
  specific topics), then mute with `prefs.json`. Grades only start steering things
  after about five weeks.
- **A feed shows only a few lines.** That's a teaser feed; see "Paywalled and teaser
  feeds" above.

## Uninstall

```bash
./uninstall.sh
```

That stops both jobs and prints the commands to remove the Keychain items. The tags
and notes it added in Reader stay until you delete them there. Then delete this
folder.

## Limits

- **macOS only.** No Linux or Windows version, and no plan for one.
- **Reader appears to drop untouched feed items after a couple of days**, so the
  program works from what is there when it runs. If your Mac is off for several days, items that
  arrived meanwhile are gone from Reader's list too.
- **It sees only what Reader reports.** Reader gives an item's current state, not
  its history, so the program keeps its own record of every tagged item. Grades
  can only be as good as that record. If Reader removes a pick from your Feed before
  you touch it, that counts as deleted (a 1).
- **Saved copies are linked only when** they come from the same site as the pick and
  you save them within 14 days of its being tagged.
- **The folder name is fixed** to `News Digest feeds` (change `DIGEST_FOLDER`).
- **The scoring prompt is written in English** and favours long-form analysis over
  news briefs. Change the prompt in `scoring.py` or mute forms if that's not your
  taste.
- **One person, one library.** It isn't built for shared accounts.
- **Topic and region labels in reports are an AI's guess** from headlines.
- Not affiliated with or endorsed by Readwise or Anthropic. It uses Reader's public
  API and Anthropic's public API under your own accounts.

## For developers

```bash
.venv/bin/python -m unittest discover -s tests   # a fake Reader; touches nothing real
.venv/bin/python -m pip install ruff && .venv/bin/ruff check .
```

How the code is laid out, what the traps are (dates, tag names, the sandbox, the
launchers), and how the background jobs are named are in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## License

[MIT](LICENSE) © P. Kerim Friedman
