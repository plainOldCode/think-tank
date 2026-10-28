# Credits

This file records design ideas read from other projects. It is not a claim that their code is in this repository.

## mainplane

Repository: [mainplane-ai/mainplane](https://github.com/mainplane-ai/mainplane)

License: MIT. Text verified from [LICENSE](https://github.com/mainplane-ai/mainplane/blob/main/LICENSE) on the default branch.

Copyright line, quoted exactly:

```
Copyright (c) 2026 Alexander Yue
```

The copyright holder named in that file is Alexander Yue.

Only ideas and design patterns are borrowed. No mainplane source code is copied or adapted in this repository.

Borrowed ideas, all still planned, described in [turbo/SHADOW_RUN.md](turbo/SHADOW_RUN.md):

| Idea | Where it was read |
|---|---|
| Append-only event log. Card state is derived from the log. | [pkg/statefile/record.go](https://github.com/mainplane-ai/mainplane/blob/main/pkg/statefile/record.go), [pkg/statefile/context.go](https://github.com/mainplane-ai/mainplane/blob/main/pkg/statefile/context.go) |
| Per-step record: request sha256, model, token usage, harness version | [pkg/statefile/record.go](https://github.com/mainplane-ai/mainplane/blob/main/pkg/statefile/record.go) |
| `idempotency_key` on a post, so a retry does not append twice | [pkg/harness/http.go](https://github.com/mainplane-ai/mainplane/blob/main/pkg/harness/http.go), [pkg/statefile/record.go](https://github.com/mainplane-ai/mainplane/blob/main/pkg/statefile/record.go) |
| Upgrade check: old data, then upgrade, then the data is still intact | [e2e/main.go](https://github.com/mainplane-ai/mainplane/blob/main/e2e/main.go) |

Not borrowed: injecting `AGENTS.md` as the rule mechanism ([pkg/harness/agents.go](https://github.com/mainplane-ai/mainplane/blob/main/pkg/harness/agents.go)), giving a worker a full shell, and shipping without a test suite. Turbo keeps the token gate, `scope`, and the scenario coverage gate.

## If code is copied later

A later change that copies or adapts mainplane code, including a substantial portion of it, must include the MIT copyright notice and the MIT permission notice alongside that code. The copyright notice is the line quoted above. The permission notice is the permission paragraph in the [LICENSE](https://github.com/mainplane-ai/mainplane/blob/main/LICENSE) file. The license states the condition in these words:

```
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
```

Ideas written in our own words do not require that notice. A copy or an adaptation of their code does.
