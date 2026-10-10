# Changelog

## v0.1.4 (2026-10-10)

[Compare changes](https://github.com/thememium/dspy-agent-tools/compare/v0.1.3...v0.1.4)

### 🩹 Fixes

- **search**: stop auto‑appending '-en' to two‑letter region codes ([50e6056](https://github.com/thememium/dspy-agent-tools/commit/50e6056cad171a3e35aa1cb4bde8068029680271))

### 💅 Refactors

- **search.py**: extend REGION_RE to allow 3‑letter region codes ([859816a](https://github.com/thememium/dspy-agent-tools/commit/859816ab626f0d0d581e2fcebef9f1322db3bf40))
- **search**: normalize region codes for DuckDuckGo ([83d8357](https://github.com/thememium/dspy-agent-tools/commit/83d83579705ddec5f1ec1da40c8e9b391cf11d43))

### ✅ Tests

- **search**: update default region logic and add region coercion tests ([dd55208](https://github.com/thememium/dspy-agent-tools/commit/dd552084373db0f1df77ba05b5e34d792cafa9e6))

### Contributors

- Edward Boswell <thememium@gmail.com>

## v0.1.3 (2026-10-10)

[Compare changes](https://github.com/thememium/dspy-agent-tools/compare/v0.1.2...v0.1.3)

### 🚀 Enhancements

- **.github/ISSUE_TEMPLATE**: add bug report issue template ([38606b2](https://github.com/thememium/dspy-agent-tools/commit/38606b24fa0e15ad85c618bced58f33082d65b96))

### 📖 Documentation

- **readme**: reformat bash_ripgrep_batch example call ([594bc33](https://github.com/thememium/dspy-agent-tools/commit/594bc337a22e8a80a766f8e183ead52b13edf400))
- **.github**: add contributing guide ([4663e4a](https://github.com/thememium/dspy-agent-tools/commit/4663e4ad8499db57c734f893ca2e5dea804d87ba))
- add SECURITY.md with vulnerability reporting guidelines ([203d410](https://github.com/thememium/dspy-agent-tools/commit/203d4109273210f56129e9f9fbeb539a93213fae))
- add MIT license ([3e8d420](https://github.com/thememium/dspy-agent-tools/commit/3e8d420548596fa6bb057ea8e444735d50d4948c))

### 🏡 Chore

- **pyproject**: enable license metadata ([8a5d6c1](https://github.com/thememium/dspy-agent-tools/commit/8a5d6c1246119dcd3f5beff71c0b749844a5caef))
- **pyproject**: remove dspy_agent_tools script entry ([f7919bc](https://github.com/thememium/dspy-agent-tools/commit/f7919bcff2c319684fdd474c9fda67d1a9fcee48))

### ✅ Tests

- add smoke tests for built distribution mappings and metadata ([0b0eb6d](https://github.com/thememium/dspy-agent-tools/commit/0b0eb6d06ec6b4685f158df74d919785fe3961dd))

### 🤖 CI

- **publish**: add workflow for automatic PyPI releases ([57a50f5](https://github.com/thememium/dspy-agent-tools/commit/57a50f5a5de24ae9143911a138e1c3edbffcbda9))

### Other Changes

- Merge pull request #5 from thememium/eboswell/chore/license-and-open-source-setup (#5) ([7bf8faf](https://github.com/thememium/dspy-agent-tools/commit/7bf8fafd23e25cde2335fb8fbf5cf030a5b35f63))

### Contributors

- Edward Boswell <thememium@gmail.com>

## v0.1.2 (2026-10-10)

[Compare changes](https://github.com/thememium/dspy-agent-tools/compare/v0.1.1...v0.1.2)

### 🚀 Enhancements

- **dspy_agent_tools**: expose bash_glob and bash_glob_batch ([ebf5635](https://github.com/thememium/dspy-agent-tools/commit/ebf5635a8ef9694a67b723c4461f174c3009adca))
- add glob-based lookup utilities to dspy_agent_tools/bash.py ([a115d07](https://github.com/thememium/dspy-agent-tools/commit/a115d074d745c982554afe28ea274c9460bf0f1d))
- **test**: add comprehensive tests for file utilities ([57fcd98](https://github.com/thememium/dspy-agent-tools/commit/57fcd984dde2ebbadbc86e5c265a360dce70bbe1))
- **dspy_agent_tools**: expose additional utility functions ([594ca19](https://github.com/thememium/dspy-agent-tools/commit/594ca1907c110fb85daf22c961f628d2e56c231c))
- **dspy_agent_tools**: add DuckDuckGo search utilities ([49c3ede](https://github.com/thememium/dspy-agent-tools/commit/49c3ede6658651a39f36ab97e256f0e39573e167))
- **dspy_agent_tools**: expose run_command tool in package exports ([e643087](https://github.com/thememium/dspy-agent-tools/commit/e64308757da7b0c859a0d74dd1a09accb0fcf397))
- **dspy_agent_tools**: add bash command runner ([d883674](https://github.com/thememium/dspy-agent-tools/commit/d883674ddba1b9b3f946e849a53717a85250e447))
- **dspy_agent_tools/files**: add multi‑mode editing with optimistic concurrency and block support ([c572b8c](https://github.com/thememium/dspy-agent-tools/commit/c572b8c7b0a0ba04df50117bcf730a5c78ac62a8))
- **files**: add read/write/edit helpers with snapshot concurrency ([0bf463a](https://github.com/thememium/dspy-agent-tools/commit/0bf463a328783cbb253e4d6916d00672a85bfeef))

### 🩹 Fixes

- **bash.py**: correct flag handling & timeout logic for rgapi ([e4f0974](https://github.com/thememium/dspy-agent-tools/commit/e4f0974f48330d5f5ad32af30f686ef6b4d29e0d))
- **files**: handle numbered prefixes in edit_file ([b3e5634](https://github.com/thememium/dspy-agent-tools/commit/b3e5634628f43c03a522bb5fe1627b29e63efa8f))

### 💅 Refactors

- **bash_agent_tools**: replace ripgrep/fd subprocesses with rgapi ([9bbb53a](https://github.com/thememium/dspy-agent-tools/commit/9bbb53a07abb89115527c997c17e3c942d46abb6))
- **dspy_agent_tools/files.py**: simplify guard for old_text ([3dd4394](https://github.com/thememium/dspy-agent-tools/commit/3dd439420b2f925a6bb19134766fdb588cfd5834))
- **dspy_agent_tools/__init__.py**: rename bash tool exports to descriptive names ([1c10b26](https://github.com/thememium/dspy-agent-tools/commit/1c10b260d84ae71be6d5f6f0af86e4bb72f1c134))
- **bash_tools**: rename tools to bash_* prefixes and add bash_fetch & bash_ls ([931166a](https://github.com/thememium/dspy-agent-tools/commit/931166ad9289362b49ec554e67b47b469f0eb34c))
- **bash.py**: unify shell execution logic and add batch utilities ([e3fb8df](https://github.com/thememium/dspy-agent-tools/commit/e3fb8df7c8cfbfc9e0b8b0ee4e38197c7af9efbd))
- **bash.py**: use Plumbum for command execution ([251f66f](https://github.com/thememium/dspy-agent-tools/commit/251f66f9e0a895fc9cef43d1c046b0e68c53c2f9))
- **example.py**: use keyword arguments for edit_file call ([277e660](https://github.com/thememium/dspy-agent-tools/commit/277e660456574ff4234636f86d94d936afa5141f))
- **files.py**: add line numbers to read_file output ([5133a3e](https://github.com/thememium/dspy-agent-tools/commit/5133a3ed7547f6eb95ce37a49218287ee0271fcf))
- **dspy_agent_tools**: expose file utilities and remove unused main ([0aa7b9f](https://github.com/thememium/dspy-agent-tools/commit/0aa7b9f1184df8df1f2d4aba8666bf12ea87d8d8))

### 📖 Documentation

- add comprehensive README with project overview and usage examples ([40a4626](https://github.com/thememium/dspy-agent-tools/commit/40a4626ea5b8fb6a404046523660a3197b376280))
- add example.py demonstrating edit_file and read_file usage ([5e7c5f0](https://github.com/thememium/dspy-agent-tools/commit/5e7c5f008ed7fd0c3c6b0ee70c454032770bdd9b))

### 🏡 Chore

- **deps**: add rgapi dependency ([ec4997f](https://github.com/thememium/dspy-agent-tools/commit/ec4997f6c1f986b9453efa3e29ae812a6697a028))
- **pyproject.toml**: drop test‑e2e task ([ad2e180](https://github.com/thememium/dspy-agent-tools/commit/ad2e1806c4bec873d0db333e81729c34a33ababc))
- **example**: remove example.py example file ([3632479](https://github.com/thememium/dspy-agent-tools/commit/3632479842dc86ed4ed4d8df75d7a0f83ca95c8c))
- **deps**: add bs4, ddgs, markdownify to dependencies ([651bebc](https://github.com/thememium/dspy-agent-tools/commit/651bebcb0e7e8273180200e76b6587a0f0cc391b))
- **pyproject**: add plumbum to dependencies ([57738de](https://github.com/thememium/dspy-agent-tools/commit/57738de5cb1612497269266d5ea8b4a8d67ce6b8))
- **pyproject.toml**: add tree-sitter-language-pack dependency ([d3e220f](https://github.com/thememium/dspy-agent-tools/commit/d3e220f6eef2337b285a40a771d9c8032ac24b1c))
- **pyproject.toml**: reformat authors and dev task for clarity ([51354ad](https://github.com/thememium/dspy-agent-tools/commit/51354adfbd502526a7e73a2c304e223ed504de2a))

### ✅ Tests

- **bash**: add tests for bash_glob and bash_glob_batch ([24f67bc](https://github.com/thememium/dspy-agent-tools/commit/24f67bc0f8299f59b70dc684bef5b54ec5571852))
- **bash**: trim trailing wall‑time line from find output ([228ecfd](https://github.com/thememium/dspy-agent-tools/commit/228ecfd26b4c725cb086766fe83609be7090709d))
- **bash**: extend ripgrep and find option testing and import fixes ([ae3a3a0](https://github.com/thememium/dspy-agent-tools/commit/ae3a3a0dfed3b3f7dfa6ec80001615ad8b331aa5))
- **search**: add unit tests for search utilities ([34f8aae](https://github.com/thememium/dspy-agent-tools/commit/34f8aae51aa0c6e010562b46c224b5b9ad56c1cc))
- **bash**: add comprehensive unit tests for bash tool module ([ad3c984](https://github.com/thememium/dspy-agent-tools/commit/ad3c9842b8adb850e40841b28483d1c926b4eb1c))

### 🎨 Styles

- **pyproject.toml**: set isort to use black profile ([11b22a1](https://github.com/thememium/dspy-agent-tools/commit/11b22a16e93737e6e6f77d299063dcbd9c7cb2a7))

### Other Changes

- Merge pull request #4 from thememium/eboswell/refactor/replace-rg-fd-with-rgapi (#4) ([246341b](https://github.com/thememium/dspy-agent-tools/commit/246341b41b903f51fe8a7e76dfc6f1ebdbc75b18))
- Merge pull request #3 from thememium/eboswell/test/add-test-coverage (#3) ([49e8b9d](https://github.com/thememium/dspy-agent-tools/commit/49e8b9d9fb39757533fca68876f42faf6e1b3cf1))
- Merge pull request #2 from thememium/eboswell/feat/add-bash-tools (#2) ([1cfee3c](https://github.com/thememium/dspy-agent-tools/commit/1cfee3c435235ce48997891f953e8e626680b433))
- Merge pull request #1 from thememium/eboswell/feat/add-file-tools (#1) ([83c120c](https://github.com/thememium/dspy-agent-tools/commit/83c120cad83f796b6ac3bc692d60c7b40f6e4f71))

### Contributors

- Edward Boswell <thememium@gmail.com>

## v0.1.1 (2026-09-15)

### 🚀 Enhancements

- **pyproject.toml, src/dspy_agent_tools**: add package metadata, CLI entry point and tooling config ([767b85e](https://github.com/thememium/dspy-agent-tools/commit/767b85e4db0a7bbc154c1f296929e916ed9f6183))

### Contributors

- Edward Boswell <thememium@gmail.com>
