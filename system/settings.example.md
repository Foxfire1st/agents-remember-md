# Agent Memory Settings Example

Copy or rename this file to `<AR_MANAGEMENT_ROOT>/system/settings.md` when scaffolding a management root.

The management root is read from `.env` in the agents-remember checkout:

```dotenv
AR_MANAGEMENT_ROOT=../ar-management
```

Relative `AR_MANAGEMENT_ROOT` values resolve from the `.env` file location.

## Derived Paths

- Onboarding root: `<AR_MANAGEMENT_ROOT>/onboarding`
- Task root: `<AR_MANAGEMENT_ROOT>/tasks`
- Local domain docs root: `<AR_MANAGEMENT_ROOT>/docs`
- System root: `<AR_MANAGEMENT_ROOT>/system`
- Sources file: `<AR_MANAGEMENT_ROOT>/system/sources.md`
- Tools file: `<AR_MANAGEMENT_ROOT>/system/tools.md`

## Default Scaffold

```text
ar-management/
├── onboarding/
│   ├── <repo-name>-onboarding/
│   └── <another-repo>-onboarding/
├── tasks/
├── docs/
└── system/
    ├── settings.md
    ├── sources.md
    └── tools.md
```

The onboarding root may contain local onboarding folders or cloned, version-controlled onboarding repositories shared by a team.