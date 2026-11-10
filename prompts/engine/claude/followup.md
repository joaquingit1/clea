You read the tail of a Slack conversation that {{user}} is already part of: {{user}} answered in it, and since then only other people wrote. Decide whether anything they wrote after {{user}}'s last message needs an answer or an action from {{user}}.

needs_him = true when, after that last message, someone asks {{user}} something (with or without a question mark: "can you confirm...", "could you help me with...", "please send me..."), asks for a decision, reports a new problem, or follows up on something {{user}} promised and has not delivered. When in doubt, true.

needs_him = false when what came after only closes or acknowledges: thanks, "ok", "perfect", laughter, an emoji, a greeting, small talk that asks nothing ("I think it went fine"), or a restatement of what {{user}} already answered.

The conversation can be in any language. `why`: one short sentence quoting the message that decides it. Return through the schema.
