
### Fix for the post-draft 400 (offline, not re-run yet)
- **Cause:** the `auto_draft` node inserted a synthetic assistant tool-call message. That message had no `reasoning_content`, and DeepSeek thinking mode rejects any later request in the same turn that carries it.
- **Fix:** `auto_draft` now calls `draft_decision` itself and ends the run with one closing text message that contains the draft and the gaps still open. No synthetic tool-call message enters the history, and no extra model call is made. We did not fake `reasoning_content`, because that depends on how one provider validates requests.
- The eval runner records the graph draft from the node update (`auto: True`) and does not count it as an LLM call.
- **Search cap:** a claim gets at most 2 mailbox searches per open gap before the agent must ask the employee, and 1 more per gap after the ask. That is 3 in total, which matches SKILL.md. Cached repeats don't count. The hero claim (4 gaps) still gets 8 searches before the ask, and the smoke run used 5.
- **Tests** (`agent/tests/test_graph_routing.py`): a `ThinkingProvider` fake reproduces the provider's 400. The tests replay smoke case 8 and a reviewer follow-up, and they cover the cap.
