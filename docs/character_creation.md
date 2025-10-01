# Character Creation Flow

The interactive `/character create` command now walks players through a six-step
process that mirrors the official SRD onboarding rules.

1. **Ability Assignment** – Choose the standard array or point-buy method and
   provide values for all six ability scores. Point-buy totals are validated
   against the 27 point budget.
2. **Race Selection** – Pick a race from the SRD catalog. Racial ability score
   increases are applied immediately and each race automatically grants its
   default languages.
3. **Class Configuration** – Choose a class, then select the exact number of
   skill proficiencies required by that class.
4. **Background & Languages** – Choose a background and satisfy any additional
   language proficiencies granted by it.
5. **Starting Equipment** – Resolve every class equipment choice (each option is
   drawn from the SRD starting packages) before continuing.
6. **Confirmation** – Review the full summary (ability scores, proficiencies,
   equipment) and confirm to persist the character.

If any SRD rule is violated (for example selecting too many skills or
overspending on point-buy) the bot returns a descriptive, actionable error. Use
the **Reset** button at any time to restart the workflow.

## Managing saved characters

* Use `/character view` to review the full details of your saved hero at any
  time. The bot will show your ability scores, proficiencies, equipment, and
  chosen background.
* Use `/character delete` to remove your stored character. You'll be asked for
  confirmation before anything is erased.
