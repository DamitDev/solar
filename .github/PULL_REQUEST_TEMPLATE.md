<!--
The supervisor copies the description block directly,
so keep it clean and use HTML comments for instructions.
-->

```
## Description
<!-- Short description of the PR, what it does, and why it's needed -->

## Changes
<!-- List of changes made in this PR -->

<!-- Uncomment if bug fix -->
<!-- ## Reproduction Steps -->
<!-- Steps to reproduce the issue -->

## Related Issues
<!-- Reference with Fixes/Resolves/Closes #issue-id -->

<!--
CROSS-PACKAGE CHECKLIST (in-monorepo, single PR — no sibling repos):
- [ ] If solar-control API routes / WS events changed, solar-host and/or
      solar-webui consumers are updated in this PR.
- [ ] If data-repository API changed, solar-control's repo:// resolution path
      is updated in this PR.
- [ ] If Docker image names / env vars changed, the aiops-k8s deployment values
      are updated in the matching deployment PR.
- [ ] If requirements.txt is touched, pyproject.toml carries the same pins
      (or `make export-requirements` was run).
- [ ] Path-filtered CI for each touched app is green.
-->
