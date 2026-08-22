# Tools

- Browser actions go through the OpenClaw gateway.
- Gmail intake goes through the OpenClaw hooks path.
- Telegram delivery goes through the OpenClaw channel configuration.
- The tracked OpenClaw configuration permits browser and web research but
  denies shell/process execution and filesystem writes. Do not ask to bypass
  those denies; use the JobOS queue, isolated repo worker, or a human review
  gate instead.
- Do not treat a tool being available as permission to send a message, submit
  a form, authenticate, upload a file, or modify a repository.
