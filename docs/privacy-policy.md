# Kimi privacy policy

_Last updated: 2026-08-29_

> **Deployment template:** Before publishing this policy, the operator must
> verify that its enabled services, retention periods, moderation features, and
> contact route match the deployment, then update the date and host it at the
> URL configured by `PRIVACY_POLICY_URL`.

Kimi is a general-purpose assistant bot for Discord communities. This page
explains, in plain language, what data a Kimi deployment handles when you talk
to it, who that data is shared with, how long it's kept, and the controls you
have. If you want the technical details, see [`privacy.md`](privacy.md).

## TL;DR

- Kimi only starts an AI conversation when you call on it: a mention, a pinged
  reply, `hey/hi Kimi`, `Kimi help`, an unmentioned message in one of its
  auto-responding threads, the optional user-installed `/chat` command, or—when
  enabled for your account—a direct message. Normal bot DMs are ignored by
  default. Personal DMs join the same private conversation as `/chat`.
- Your messages to Kimi go to the AI provider that powers its replies. Optional
  services receive the input needed for the feature you request. Browser tasks
  and network-enabled code can also contact sites or services chosen by the task.
- Conversation history auto-deletes after **30 days**. When long-term memory is
  available, it is on by default; you can opt out with `/memory opt-out`, or
  wipe it any time with `/privacy`.
- Your data is **never sold or used for ads**.
- `/privacy` deletes Kimi's local conversation history, workspace files,
  persistent browser profile, video sessions, and personal memory. For video
  sessions Kimi also requests deletion of every known Gemini Interaction and
  uploaded File; it cannot erase Discord messages or guarantee removal from a
  provider's safety logs or backups.

## When Kimi is listening

Kimi only starts a conversation when you call on it: by @mentioning it,
replying with the ping on, saying `hey/hi Kimi` or `Kimi help`, or posting in an
auto-responding thread it started. Paused threads require an explicit
invocation. It does **not** store ordinary channel conversation as chat history.

While it works on something you asked for, Kimi may read recent messages from
the current channel or search other channels that you and the bot can both read,
except channels the operator excluded. That gives the AI useful context, but it
does not turn those messages into Kimi conversation history just because they
were retrieved. If your invoked message contains a Discord message link, Kimi
may also read that exact linked message when it is in this server and both you
and the bot can access it. Channel links, channel mentions, and recognized
channel IDs may be translated into channel, thread, parent, and category names.
These automatic hints are temporary context, not transcript copies.

**Kimi ignores ordinary direct messages by default.** An operator can enable
personal DMs for approved users. When enabled for you, a DM joins the same
private personal transcript as `/chat`; otherwise it is dropped without a reply,
transcript write, or provider call. `/chat-reset` clears that transcript but
keeps long-term memory and workspace files; `/privacy` remains the full deletion
control.

## What Kimi collects

Depending on the features a server enables, Kimi handles:

- **Your messages to it**: the text you send when you invoke Kimi, and any
  images or files you share with it in that message.
- **Basic Discord identifiers**: your user ID and display name, and the
  server/channel the message was in, so Kimi can reply in the right place and
  keep track of a conversation. If you ask Kimi to start a managed thread, it
  also stores your user ID as that thread's initiator so that only you or a
  moderator can close it or change its reply mode.
- **Your server profile, when someone asks about you**: Kimi can look up a
  member of this server and read their username, display name, avatar, account
  and join dates, and roles. Anyone in the server can already see all of that.
  The lookup goes to the AI provider as context for that one reply and is not
  stored.
- **Usage metadata**: token counts and attributed LLM/tool-provider costs for a
  turn, plus short-lived counters used to enforce bounded-tool limits. None of
  this includes the content of your messages, code, tool queries, or results.
- **Video-session metadata, if enabled**: source kind, safe display filename or
  relative workspace locator and byte size (or a canonical YouTube URL), opaque
  local and Google File/Interaction identifiers, model, owner/conversation
  scope, counts, and timestamps. Uploaded bytes, Discord CDN URLs, questions,
  and answers are not duplicated into local session tables.
- **Messages in channels Kimi can read**: when someone asks Kimi
  something, it may pull recent messages from that channel, or from channels
  the operator has configured for search. It may also retrieve an exact message
  linked in an invoked message after checking the requesting user and bot can
  read it. That can include messages you wrote that were never addressed to
  Kimi. They go to the AI provider as context for that one reply, and are not
  saved to Kimi's transcript or personal memory merely because they were
  retrieved.
- **Messages staff teach to Kimi**: staff can deliberately turn a server
  message into shared community knowledge or a reusable shared skill. The
  quoted message is sent to the AI provider for that task. What Kimi learns may
  be stored in community memory or a shared skill. If the server configures a
  staff learn-log channel, Kimi attempts to post a summary there; the log is
  optional and a failed post does not undo the learned item.
- **Staff configuration proposals**: modules can store proposed configuration,
  its exact baseline, staff proposer and decider IDs, a decision reason, and the
  Discord review-message location. These operational records have no automatic
  expiry and are not removed by `/privacy`. The review channel also receives a
  Discord card with the proposer, summary, a bounded preview, and the decision.

When personal DMs are disabled, or you are not on the approved access list,
messages sent directly to Kimi are ignored without being read into a turn,
stored in its transcript, or sent to an AI provider. Kimi may also check whether
you hold a role or channel permission that a command depends on, such as the one
that lets a moderator close a managed thread.

## How Kimi uses your data, and who it's shared with

- **Answering you.** Your message and recent conversation are sent to the AI
  provider that powers Kimi's replies. Like any cloud AI service, that provider
  may also receive recalled personal or community memory and tool results needed
  for the turn, and may process or log that input under its own policies.
- **Coding work.** If the coding agent is enabled, its provider receives the
  task objective, acceptance criteria, bounded conversation context, and files
  or tool results the worker reads. This may be a different provider from chat.
- **Long-term memory.** When enabled, conversation slices, durable facts, and
  recall queries are sent to the configured Hindsight service for users who
  have not opted out. That service may be self-hosted or hosted by a third
  party. A self-hosted Hindsight deployment may also use a separate model
  endpoint selected by the operator for memory processing.
- **Safety checks.** If conversational content moderation is enabled, your
  message to Kimi and Kimi's draft reply may be checked by a moderation service
  before sending.
- **Custom personas.** If you ask Kimi to adopt a persona, your request and
  your display name are sent to a language model that rewrites it into a
  community-appropriate persona. The result is stored against your user ID and
  used in your future chats until you clear it or delete your memory.
- **Internet search and page reading.** If enabled, Kimi sends a search query,
  search filters, or URLs to the configured search provider. Built-in providers
  include TinyFish, Exa, and Brave. Opening a public URL also shows up as a
  normal web request to that website.
- **Persistent browser.** If enabled, sites receive normal browser requests and
  anything entered or submitted during the task. They can set cookies and site
  storage in your private browser profile. Depending on operator configuration,
  traffic uses either the bot host's routes or a separate network boundary.
- **Network-enabled code.** If enabled, `run_code` and coding jobs can contact
  destinations chosen by generated code and can send task inputs or readable
  workspace data. Host-network mode can reach services available through the
  bot host; isolated-network mode uses a boundary configured by the operator.
- **Image generation and editing.** If enabled, Kimi sends the image prompt,
  output settings, and any workspace reference images selected for the edit to
  OpenAI's image service. The returned PNG is stored in your workspace for
  delivery and later edits.
- **Wolfram|Alpha.** If enabled, Kimi sends a bounded, single-line computation
  query and optional units choice to Wolfram|Alpha.
- **Video understanding.** If enabled and you ask about a public YouTube video,
  Kimi sends its URL and your questions to Google's paid Gemini API. If you
  select a Discord attachment or workspace video, Kimi streams those video
  bytes to Google's Files API before analysis. Google temporarily stores the
  File and Interaction chain so follow-ups can continue without re-uploading.
- **Community learning.** Staff can use the process described above to store
  shared knowledge in Hindsight or in a shared skill. This is separate from
  your personal memory and is managed by staff.
- **Operator-added tools.** The server operator may install plugins or scripted
  tools that contact additional services when used. The operator is responsible
  for documenting those services and limiting the data each tool sends.
- **Operator-installed modules.** The server operator may install application
  modules that receive Discord events or content, including content not
  addressed to Kimi when the module's function requires it. Before enabling
  such a module, the operator is responsible for making its privacy disclosure
  available to affected users, including what it processes, where it sends
  data, and how long it retains the result.

Kimi does **not** sell your data, use it for advertising, or share it outside
the configured services and tools needed to answer you.

## How long Kimi keeps your data

- **Conversation history: 30 days.** Your messages to Kimi (and any images in
  them) are automatically deleted 30 days after a conversation goes quiet.
- **Long-term memory: until you delete it.** If the server has long-term memory
  enabled, it is on for you by default, and Kimi can remember durable facts you
  share to personalize future chats. You can opt out of future use and
  retention with `/memory opt-out`, or wipe existing memory any time with
  `/privacy` (the **Delete memory** button).
- **Files you create with Kimi: 7 days.** Files in your personal workspace are
  removed after 7 days of inactivity. Guild-chat workspaces are kept separate
  per server, so files you make in one community aren't visible from another.
  The optional personal user app instead uses one workspace shared across that
  user's `/chat` and enabled DM conversations.
- **Browser profile: 7 days.** If the interactive browser is enabled, the
  cookies, site storage, cache, history, and screenshots from tasks Kimi does
  for you live in a profile that is private to you. It is removed after 7 days
  of inactivity, or sooner by **Delete my data**. The sites Kimi opens on your
  behalf see that visit, and can set cookies that stay in your profile until it
  expires or you delete it.
- **Video sessions: up to 24 hours idle locally.** Kimi removes local session
  access after that and queues every known Gemini Interaction and uploaded File
  for provider deletion. Google documents Files API retention up to 48 hours
  and paid-tier Interaction retention up to 55 days, and may retain limited
  safety/security records under its terms.
- **Personal skills: until you delete them.** Reusable instruction skills you
  create are stored separately from the expiring workspace. Ask Kimi to delete
  a personal skill when you want it gone.
- **Community knowledge and private shared skills: until staff remove them.**
  These are shared server resources. They are not part of your personal memory
  and are not removed by `/privacy`.
- **Diagnostic logs: size-limited.** If diagnostic logging is on, technical
  logs live in rotating files that discard the oldest file as they fill. The
  default mode records metadata only, though operators can choose modes that
  also record message and response text, retrieved channel context, and tool
  inputs and results. `/privacy` does not edit these files.
- **Usage metadata: kept indefinitely.** The LLM and paid-tool cost accounting
  records (which contain no message text) are kept for cost tracking and are
  not on the 30-day clock.
- **Bot blocks: until removed.** If you or a moderator block the bot from
  responding to you, Kimi keeps your user ID plus the block's creator, reason,
  and timestamps until the block is removed. Blocks are not removed by
  **Delete my data**.
- **Discord staff records: controlled by server staff.** Learning and
  configuration-proposal cards are messages in Discord. They
  remain until staff or Discord remove them and are not covered by Kimi's local
  retention sweep or `/privacy`.

## Your controls

- **`/privacy` → Delete memory**: wipes your personal long-term memory, turns
  future memory off (the same as `/memory opt-out`), and clears your custom
  persona. Use `/memory opt-in` if you later want to turn memory back on.
- **`/privacy` → Delete my data**: does everything above and also immediately
  deletes Kimi's local copy of conversations you started, your messages in
  conversations started by someone else, your workspace files, browser profile,
  and video sessions. For those sessions Kimi also requests deletion of every
  known stored Gemini Interaction and Files API upload and retries temporary
  failures. If Gemini access is unavailable, local deletion still completes.
  Provider deletion remains independently queued; this does not keep your account
  activity paused. If you started a shared conversation, Kimi's local copy of
  that whole conversation is removed, including messages other people added to
  it; their other conversations, workspaces, preferences, and personal memory
  are left alone.
- **What `/privacy` cannot delete**: messages or files stored by Discord;
  provider safety logs, legally required records, backups, and copies outside
  the stored Gemini video Interactions Kimi knows how to delete; diagnostic
  logs; community knowledge; shared or personal skills; usage and rate-limit
  records; module configuration proposals; learning and proposal-review
  messages; blocks; data retained by installed modules except where a module
  documents its own deletion path; or your saved consent preference. Each has
  its own lifecycle, described above.

Deletion waits for any interaction already in progress, and blocks new activity
for you until the required local deletion finishes. Your confirmation is saved
before deletion starts, so a restart cannot lose it. If a required local or
memory service is temporarily unavailable, the request stays pending; retry
`/privacy` or ask staff for help. Gemini video-provider deletion is different:
local video-session deletion can finish while content-free File/Interaction
cleanup rows remain queued for later retry.

- **`/memory status`**: see whether memory is on for you.
- **`/memory opt-out`**: stop Kimi from remembering anything new about you.
- **`/memory opt-in`**: turn memory back on after opting out.
- **Block Kimi**: ask Kimi to block you, and it will stop responding to you.
- **Privacy prompt**: if the consent prompt is enabled on your server, you can
  tap **Decline** the first time, and nothing you said is sent to the AI
  provider or stored as a conversation.

## Who can see your data

The bot operator can access the database, workspace files, diagnostic logs, and
the configured Hindsight service through the deployment's credentials. Anyone
can check their own usage totals with `/usage`. Discord staff can use `/moderation` and view
other users' usage totals. Staff with access to configured learning or
proposal-review channels can see the event cards posted there. Installed
modules can add other staff-visible records as described in their own privacy
documentation. None of these commands expose private transcripts or personal
memory.

## Age

Kimi is intended for users who are **old enough to use Discord in their
country, and never under 13**.

## Changes to this policy

We may update this policy as Kimi changes. Material changes will be noted with
a new "last updated" date.

## Questions

Reach out to the bot owner, or to the server staff in your community, with any
privacy questions or requests.
