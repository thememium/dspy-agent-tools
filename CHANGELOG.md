# Changelog

## v0.1.9 (2026-10-31)

[Compare changes](https://github.com/thememium/dspy-agent-tools/compare/v0.1.8...v0.1.9)

### 🩹 Fixes

- adds mcp server retry for read only tools (#10) (#10) ([9fae39b](https://github.com/thememium/dspy-agent-tools/commit/9fae39b9e8ea5037740ae46543da4686142de812))

### Contributors

- Edward Boswell <thememium@gmail.com>

## v0.1.8 (2026-10-31)

[Compare changes](https://github.com/thememium/dspy-agent-tools/compare/v0.1.7...v0.1.8)

### 🚀 Enhancements

- **cli**: add arguments parsing, usage hints, and docs updates ([1e09f47](https://github.com/thememium/dspy-agent-tools/commit/1e09f4791849c19d72b86db9280aac230e583893))
- **cli**: add max_output_chars to cap command output size ([bcf5505](https://github.com/thememium/dspy-agent-tools/commit/bcf5505d5440cdbb3ce95acc22d524e3bad56fbd))
- **cli**: add include_search flag to factory and toolbox ([980f8bc](https://github.com/thememium/dspy-agent-tools/commit/980f8bc240fe3ceec73835413399da9cd77e5bb6))
- **cli**: support help flag as command and add no_help command ([219a7ef](https://github.com/thememium/dspy-agent-tools/commit/219a7efe671d05f3a6b8e486814751fff1af6eff))
- **cli**: support custom help flag for discovery ([22a337c](https://github.com/thememium/dspy-agent-tools/commit/22a337c61ed05b9e00fa39995ae011ffe899e379))
- **cli**: add JSON flag support and legacy command ([5a87854](https://github.com/thememium/dspy-agent-tools/commit/5a8785454258b502be93d5d1bbbdd7116afb25dd))
- **cli**: add JSON support flag to CLI discovery and command execution ([583dad5](https://github.com/thememium/dspy-agent-tools/commit/583dad567689c6d33864886f6972b76a9ae2796d))
- **dspy_agent_tools/__init__**: expose CLI helper classes Arg, Argument, Command, Option ([4a534a3](https://github.com/thememium/dspy-agent-tools/commit/4a534a3c04f22f4dd4c454944a1f8c07b217b2db))
- **cli**: add explicit command definitions and refactor CLI tool builder ([9ab8315](https://github.com/thememium/dspy-agent-tools/commit/9ab8315b2f4e7a8aea1c28de14966c9048478521))
- **cli**: add lazy discovery of executable commands via --help ([318dc03](https://github.com/thememium/dspy-agent-tools/commit/318dc0392ad29dc49ecb1580c76e35bc241d3b95))
- **dspy_agent_tools**: expose CLI and CLITools in package __all__ ([07638f0](https://github.com/thememium/dspy-agent-tools/commit/07638f059545ab517903b4821adc051fa7c4af92))
- **bash.py**: add optional env to subprocess execution ([d58beab](https://github.com/thememium/dspy-agent-tools/commit/d58beab2d9514cd720285461e906bacefeef6e99))
- **dspy_agent_tools**: add CLI discovery tools ([4066217](https://github.com/thememium/dspy-agent-tools/commit/4066217763a12853c18e28fa22034f391bc23014))

### 🩹 Fixes

- **test_cli**: correct flag kind, enrich error suggestions ([7e6ca75](https://github.com/thememium/dspy-agent-tools/commit/7e6ca75ce2eb7c715a10e4a7c57d91ba23201e54))

### 💅 Refactors

- **dspy_agent_tools/cli.py**: add include_search flag to control search_commands, adjust discovery behavior and update documentation ([3917c21](https://github.com/thememium/dspy-agent-tools/commit/3917c212a5632953b9b5e5a82904c20628f8b09e))
- **dspy_agent_tools**: relocate Arg to cli module, remove from exports ([b83d3cc](https://github.com/thememium/dspy-agent-tools/commit/b83d3cccaabf956724f05a9aaee2c325b0d1cca5))
- **dspy_agent_tools**: remove unused Arg from CLI imports ([17e6b59](https://github.com/thememium/dspy-agent-tools/commit/17e6b59c5f068d642f5e75ba37588c1669d2ea46))
- **cli.py**: remove legacy Arg from public API and update docs ([8e028ae](https://github.com/thememium/dspy-agent-tools/commit/8e028aec58642575f43d977113ca1bdf3968bbd5))
- **cli**: update Command constructor usage to keyword arguments ([8e10539](https://github.com/thememium/dspy-agent-tools/commit/8e10539f157f214729cc12e3f860ce7416d54a87))
- **dspy_agent_tools/cli.py**: make CLI argument classes keyword‑only and rename path to command ([73e1848](https://github.com/thememium/dspy-agent-tools/commit/73e1848d5bb8fb55e8d2d694b1e6dbb2f589b347))
- **cli.py**: add pragma no cover to defensive flag name check ([9ef48dc](https://github.com/thememium/dspy-agent-tools/commit/9ef48dcd51cc04fa0cdb3a097864a00926f1022b))

### 📖 Documentation

- **contributing**: improve project structure diagram and add import section ([5774e17](https://github.com/thememium/dspy-agent-tools/commit/5774e179944c51bdefeb79a57b2b2cbe27f6df2f))
- clarify CLI tool behavior and include_search option ([fcc18ed](https://github.com/thememium/dspy-agent-tools/commit/fcc18ed09101538ce52d55bc87fe2440dcdb2725))
- **readme**: add detailed help_flag description for better CLI usage ([55849f9](https://github.com/thememium/dspy-agent-tools/commit/55849f95a47a6adfd6adc2a7b49e09af8ce3a697))
- **README**: clarify Argument and Option as the only public declaration helpers ([0697452](https://github.com/thememium/dspy-agent-tools/commit/0697452dc1b88928d992e84181bbf49bc9a4d1fe))
- add json_flag description to README ([dbddbd8](https://github.com/thememium/dspy-agent-tools/commit/dbddbd8734a02e8d631768e8518c14621e1baf64))
- **README**: update CLI example syntax for clarity and consistency ([68bde6d](https://github.com/thememium/dspy-agent-tools/commit/68bde6d47ccc5b55fec21f18a86e54b4dae309ac))
- **README**: add explicit CLI commands section and update CLI description ([961eb1c](https://github.com/thememium/dspy-agent-tools/commit/961eb1cbf6769a6adaacf2f7a90ec19fde7c6490))
- add CLI documentation and update tool families list ([17a360d](https://github.com/thememium/dspy-agent-tools/commit/17a360d76e64d4f4453f1bcc9b71ecfc2d40f242))

### ✅ Tests

- extend smoke test to include Arg, Argument, Command, Option ([13c4b38](https://github.com/thememium/dspy-agent-tools/commit/13c4b3886c2519f4c4c49fe8bff4ba2af73793c7))
- add explicit command and argument handling tests ([79b7657](https://github.com/thememium/dspy-agent-tools/commit/79b76578eddd84d74783d41dfaec7f07ddcdae07))
- **test_cli**: add parse_flag helper to simplify flag parsing tests ([9db9702](https://github.com/thememium/dspy-agent-tools/commit/9db9702570df733844cedeafa7a6c802bef2d514))
- add comprehensive edge case tests for CLI parsing and discovery ([0401673](https://github.com/thememium/dspy-agent-tools/commit/04016734621af4377cd79b8cbf2706200a14bb13))
- **cli**: add extensive test suite for CLI module ([ec07069](https://github.com/thememium/dspy-agent-tools/commit/ec070690232b4356ed530ea859694ab10d4ccf74))
- **test_smoke**: include CLI and CLITools in EXPECTED_EXPORTS ([767e589](https://github.com/thememium/dspy-agent-tools/commit/767e589fe0e5e33477248f9f991ccea3c48ff737))

### Other Changes

- Merge pull request #9 from thememium/eboswell/feat/add-cli-tool-caller (#9) ([324795b](https://github.com/thememium/dspy-agent-tools/commit/324795b738d7065adb72b920bfca89671acd83e6))

### Contributors

- Edward Boswell <thememium@gmail.com>

## v0.1.7 (2026-10-30)

[Compare changes](https://github.com/thememium/dspy-agent-tools/compare/v0.1.6...v0.1.7)

### 🚀 Enhancements

- add tool search modes (#8) (#8) ([9b50e53](https://github.com/thememium/dspy-agent-tools/commit/9b50e53eac6de73f6920853ce003cc8c12a03284))
- add mcp tool (#7) (#7) ([10ee278](https://github.com/thememium/dspy-agent-tools/commit/10ee27878b45efc888d0e9d0ca2bc4f1be2f4b97))

### Contributors

- Edward Boswell <thememium@gmail.com>

## v0.1.6 (2026-10-30)

[Compare changes](https://github.com/thememium/dspy-agent-tools/compare/v0.1.5...v0.1.6)

### 🚀 Enhancements

- **search**: add failover search with retries and status ([12c6983](https://github.com/thememium/dspy-agent-tools/commit/12c6983244129ba4c95cb8e73085311622efc7cd))

### 📖 Documentation

- **readme**: update Search section to show multi‑engine web and news search with failover ([6b489c2](https://github.com/thememium/dspy-agent-tools/commit/6b489c22abfb766a2fd298205d35a48e22b79c70))

### ✅ Tests

- **search**: extend test coverage for search utilities ([f027d06](https://github.com/thememium/dspy-agent-tools/commit/f027d06dfe88ff6985fe175f557a0d2275de0c12))

### Other Changes

- Merge pull request #6 from thememium/eboswell/feat/improved-web-search-engines-retries-failovers (#6) ([1293280](https://github.com/thememium/dspy-agent-tools/commit/12932809774bb3bd2cd1eafc497b4cfb3ae37e21))

### Contributors

- Edward Boswell <thememium@gmail.com>

## v0.1.5 (2026-10-19)

[Compare changes](https://github.com/thememium/dspy-agent-tools/compare/v0.1.4...v0.1.5)

### 🚀 Enhancements

- **bash.py**: remove output truncation and switch to token‑based junk detection ([4837577](https://github.com/thememium/dspy-agent-tools/commit/4837577a6463ba61b28d0fd482dc7efcf1065127))

### 🩹 Fixes

- **bash**: eliminate old output cap and correct content filtering ([0b172f1](https://github.com/thememium/dspy-agent-tools/commit/0b172f15346a1f79172cbaec2e463bdff3677e67))

### 📖 Documentation

- **README**: change output truncation behaviour to return full output ([6ccbc6a](https://github.com/thememium/dspy-agent-tools/commit/6ccbc6aa784e0dcf30a54438ea635b41eb9ec7f8))

### 🏡 Chore

- **deps**: update dependency versions to newer releases ([9468237](https://github.com/thememium/dspy-agent-tools/commit/94682379b3f881166788d36dcf898843e11452b3))

### ✅ Tests

- **bash**: add tests for junk element detection and removal of unwanted tags, attributes, and comments ([baf9fa2](https://github.com/thememium/dspy-agent-tools/commit/baf9fa28e6942c4bf9d7f87ecd613bd02de3f570))

### Contributors

- Edward Boswell <thememium@gmail.com>

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
