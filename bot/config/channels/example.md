---
# Optional frontmatter is read fresh each turn and stripped before this fragment
# fills <channel_instructions>.
#
# Pre-activate searchable tools in this channel without browse_tools. Pins never
# widen privileges, and a blocked tool always wins over a pin.
pinned_tools: [discord_text_search]
blocked_tools: [teach]
#
# Thread switches. Both are tri-state: omit the key to inherit the server value
# (and then the default, on), or set a literal true/false here. A typo is treated
# as "not set here" rather than flipping the channel.
#
#   thread_handoff:         may the bot open threads from this channel at all?
#   thread_auto_respond:    does a thread opened here answer every message, or
#                           only when mentioned? The handoff initiator, staff,
#                           or someone with Manage Threads can change its mode;
#                           this is only the starting point.
thread_handoff: true
thread_auto_respond: true
#
# Optional automatic thread handoff. This section has an effect only when the
# deployment also sets THREAD_AUTO_HANDOFF_ENABLED=true. Set auto_thread_always
# to true to move every eligible reply, or leave it false and use either
# threshold below. Thresholds are independently optional.
auto_thread_always: false
auto_thread_min_lines: 4
auto_thread_min_chars: 600
---
You are in the #general channel of this community.

- This is a casual hangout channel. Keep responses brief and conversational.
- Match the energy of the room; if people are joking around, be playful.
- Move detailed troubleshooting into a thread so the main channel stays readable.
