# Implementing Issues with Agents

This serves as a guide for managing agents with the correct prompts and context to achieve the best solution for a given issue in the roadmap. It is intended for both supervisors (human programmers) and agents.

## Key Principles

There are some key principles that must be followed.

- The supervisor and the agent should always understand the issue and the goal, and share the same perspective on the implementation approach and overall plan.
- The agent must always have the correct context. The issues already provide some guidance, but the supervisor should always check whether additional information is needed. This includes pointing the agent at the relevant repo(s) with `@` references and ensuring it reads the source files mentioned in the issue's "Additional Notes" section.
- The supervisor should always provide architectural decisions and guidelines. Agents should not come up with their own solutions and decisions.
  - If the agent reaches a decision point, it should ask the supervisor for guidance. The agent may propose solutions, but we prefer the supervisor's own ideas. Agents should only go with their own suggestions if the supervisor agrees with them.
  - Agents should avoid assumptions.
  - Supervisor and agents should always rely on best practices and standards.
  - Agents should verify that both their own and the supervisor's ideas align with common practices and standards.
- The supervisor and agent will work in sync: the agent will stop between solution steps and let the supervisor review and verify its progress.
- The supervisor and agent dynamically adjust the plan if necessary, but only after consultation.

## Branching Convention

- **Agent** creates a feature branch from `master`, named after the issue ID: `feature/{issue-id}` (e.g. `feature/S-014`, `feature/D-007`).
- If an issue touches multiple repos, the **agent** creates the same-named branch in each repo.
- One branch per issue. Do not mix unrelated changes.

## Workflow

The workflow is as follows:

1. **Supervisor** prompts the agent with the issue text and provides additional info when they find it necessary. For issues that touch multiple repos, the supervisor should specify which repo to work on first and provide context for each one.
2. **Agent** generates a plan for the issue with steps. Steps should be logical and easy for the supervisor to verify between stops. The agent should ask questions if clarification is needed.
3. **Supervisor** reviews the plan, provides necessary clarifications and adjustments. Agent and supervisor iterate on the plan until it is approved by both parties.
4. **Agent** starts the implementation, goes through the todo steps one by one, and lets the supervisor review and verify progress between each step. (Steps should be designed with the understanding that there will be a stop between each one, so it's good practice to bundle small trivial tasks into a single todo step.)
5. **Agent** verifies consistency, runs linting and formatting checks as described in the given repository's rules and guides, and runs tests if applicable.
6. **Agent** verifies the implementation by running the application or service in a local environment, if possible.
7. **Supervisor** reviews the implementation and provides necessary clarifications and adjustments, which the **agent** will implement. This continues until the issue is resolved to the supervisor's satisfaction.
8. **Agent** will update the repo documentations if necessary.
9. **Agent** provides a PR title and description for the supervisor to review and approve.
10. **Supervisor** commits the changes to the repository, creates the PR, and merges it if approved. The agent should never commit or push directly.

## Prompting the agent

The supervisor prompts the agent with the issue text and provides additional info when they find it necessary. For issues that touch multiple repos, the supervisor should specify which repo to work on first and provide context for each one.

- For additional info, the supervisor will provide some ideas, and talk about potential concerns which would need extra attention.
- For references, the supervisor will provide files or paths which is related to the issue and possibly needed for the implementation.
- For context, rules, and guides, the supervisor will provide files or paths which serves as a general context for the agent about behavior best practices, standards, and rules - as the title suggests.

The prompt template can be found in the `templates/agent-prompt-template.md` file.
