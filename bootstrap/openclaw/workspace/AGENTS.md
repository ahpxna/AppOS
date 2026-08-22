# OpenClaw workspace agents

- Keep automation and human browser sessions separate.
- Use the dedicated `openclaw` OS user when available.
- Treat browser actions as side effects that must stay inside approved flow.
- Proceed without asking for confirmation for safe reads, summarisation,
  planning, and evidence checks. Ask one concise question only when required
  input is absent or when a requested action would send, submit, authenticate,
  upload, modify external data, or make an unsupported candidate claim.
- Never expose chain-of-thought, secrets, browser cookies, gateway tokens, or
  raw private profile data in a response. Share only concise evidence, status,
  decisions, and next actions with other agents.
