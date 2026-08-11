# Signing-key recovery

Private risk and audit-anchor keys are intentionally excluded from Git, database dumps, application backups and logs. Public verification keys are included in the verified asset backup.

The owner must keep encrypted private-key recovery material in an independent secret store. Recovery is allowed only while the DEMO execution gate is absent, every broker writer is stopped, trading state is `LOCKED`, and current database/config/release backups have passed checksum verification.

If a private key is lost or suspected compromised:

1. keep the gate absent and preserve all existing events, anchors and public keys;
2. revoke the affected key recovery copy and create a fresh local keypair using the provisioning path;
3. archive the previous public key with its validity interval and last signed hash;
4. install the new public key into verifier services and create a signed transition record in the operational evidence store;
5. rerun boundary, signature-negative, backup and restore drills before any future DEMO activation.

Never copy a recovered private key into the repository, NAS market archive, a command line, task transcript or general backup archive. REAL credentials/keys are outside this procedure and remain unsupported.
