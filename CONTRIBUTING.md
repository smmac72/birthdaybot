
# Contributing to BirthdayBot
Thanks for your interest in contributing!
I do welcome pull requests, bug reports, and translations.  

## How to contribute
1.  **Fork** the repository
2.  **Create a new branch**
```bash
git checkout -b feature/my-improvement
```
3.  **Make your changes**
- Follow the existing style
- Keep handlers async (`async def`)
- Add comments for non-obvious logic
4.  **Run locally**
```bash
python -m bot.main
```
or with Docker:
```bash
docker compose up -d
```
5.  **Test before pushing**
- Ensure bot starts without exceptions
- Verify menus and settings work correctly
6.  **Commit & push**
```bash
git add .
git commit -m "fix: ISSUE-X"
git push origin feature/my-improvement
```
7.  **Open a Pull Request** to the `stage` branch
(`main` is used only for stable releases)
CI/CD will auto-deploy stage builds to the staging server.
---
## Translations

1. Copy `bot/locales/en.yaml` → `bot/locales/xx.yaml`
2. Translate values (keys must remain the same)
3. Add your language code to `available_languages()` in `i18n.py`
4. Test the app, search for any missing localization keys
---

## Testing checklist
Could do a larger one. Probably will do later TODO
- [ ] `/start` registration works
- [ ] Settings menu updates correctly
- [ ] Alerts reschedule after changing timezone
- [ ] Friends and group joins functional
- [ ] Wishlist add/remove works

## Code style
- 4 spaces indentation
- snake_case naming
- prefer f-strings
- use type hints (`-> None`, `Optional[str]`, etc.)

## Docker guidelines
- Keep Dockerfile and `docker-compose.yml` generic
- Use `.env.*` for secrets
- Ensure both `birthdaybot-*` and `adminbot-*` containers start cleanly

## Need help?
Create an Issue or contact me via Telegram (see “About” menu).

Thank you lads!