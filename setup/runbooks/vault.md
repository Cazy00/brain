# vault/ — encrypted sensitive notes

`knowledge/vault/` holds notes too sensitive for plaintext on GitHub (health,
finances, IDs, private matters about people). Ciphertext is committed — the
knowledge stays permanent and backed up — but only this machine's key can
read it. Lint blocks any plaintext `.md` inside vault/ and any
`sensitivity: private` note outside it.

## One-time setup

```sh
brew install age
age-keygen -o ~/.config/brain/vault-key.txt    # keep OUT of the repo
# Back the key up in macOS Keychain (the key IS the vault — do not lose it):
security add-generic-password -a "$USER" -s brain-vault-key \
  -w "$(cat ~/.config/brain/vault-key.txt)" -U
```

The public key (recipient) is committed at `setup/vault-recipient.txt` —
encryption needs only this, so any machine can add vault notes; only machines
with the private key can read them.

> Status 2026-07-22: key generated, backed up in Keychain (item
> `brain-vault-key`), round-trip tested. This setup section is already done.

## Encrypt a note into the vault

```sh
age -r "$(cat setup/vault-recipient.txt)" -o knowledge/vault/<name>.md.age <plaintext-file>
rm <plaintext-file>          # plaintext never gets committed
```

## Read a vault note

```sh
age -d -i ~/.config/brain/vault-key.txt knowledge/vault/<name>.md.age
```

## Recover the key on a new machine

```sh
mkdir -p ~/.config/brain
# Redirect to a temp file and move it into place. A bare
# `... -w > ~/.config/brain/vault-key.txt` truncates the destination BEFORE
# security runs, so a missing keychain item wipes the key you meant to restore.
security find-generic-password -a "$USER" -s brain-vault-key -w > /tmp/vault-key.$$ \
  && mv /tmp/vault-key.$$ ~/.config/brain/vault-key.txt \
  || { rm -f /tmp/vault-key.$$; echo "no vault key in this machine's Keychain"; }
```

(Keychain syncs via iCloud Keychain if enabled; otherwise store a copy in your
password manager as a second backup.)
