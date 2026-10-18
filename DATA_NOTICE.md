# Data notice

All data in this repository is **synthetic**. It is generated locally by
`governed_agent/datagen.py` using Faker and NumPy with a fixed seed. No real
customer, account, transaction, or institution data is used, and no names in the
data refer to real people or organizations. Any resemblance is coincidental.

The generator deliberately seeds data-quality defects (duplicates, non-canonical
currency codes, missing categories, sign errors, future-dated rows) so the agent
has something to fix.
