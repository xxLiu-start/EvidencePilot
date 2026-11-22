# Claude API pricing and prompt caching

- **Resource ID:** 09
- **Publisher:** Anthropic Claude Platform
- **Official source:** https://platform.claude.com/docs/en/about-claude/pricing
- **Purpose:** Estimate input/output, cache-write and cache-hit costs.

## Reading notes

The pricing guide describes model pricing and prompt-caching options, including automatic caching and explicit breakpoints. Estimate effective cost using real cache hit rates, stable prompt prefixes and request patterns.

## Enterprise selection questions

- How much input is repeated
- Can cache hits offset cache-write costs
- What invalidates cache breakpoints
- Does the estimate include input, output, cache and tool costs?
