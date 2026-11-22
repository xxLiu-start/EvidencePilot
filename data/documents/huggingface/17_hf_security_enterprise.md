# Hugging Face Hub security

- **Resource ID:** 17
- **Publisher:** Hugging Face Documentation
- **Official source:** https://huggingface.co/docs/hub/en/security
- **Purpose:** Review repository access, tokens, MFA, scanning and enterprise controls.

## Reading notes

The security guide describes private repositories, access tokens, resource groups, MFA, commit signing and malware/pickle scanning. Treat model supply-chain security as part of admission control; review remote code before execution.

## Enterprise selection questions

- Are repositories private with least-privilege access
- Are tokens rotated and audited
- Are model files and remote code scanned
- Are model origin, version and license recorded?
