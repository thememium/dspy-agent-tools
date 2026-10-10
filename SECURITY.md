# Reporting a Vulnerability

To report a security vulnerability, please email boswell.labs@gmail.com.

We take security seriously and will respond to security reports within 48 hours. Please include as much detail as possible about the vulnerability, including:

- A description of the vulnerability
- Steps to reproduce the issue
- Potential impact
- Suggested fix (if any)

While the discovery of new vulnerabilities is rare, we also recommend always using the latest version of dspy-agent-tools to ensure your application remains as secure as possible.

## Security Considerations for dspy-agent-tools

dspy-agent-tools is a library of tool functions for [DSPy](https://dspy.ai) agents — shell execution, file manipulation, and web search — that you hand to `dspy.ReAct` or `dspy.RLM`. Because these tools are executed by LLM-generated decisions, please be aware of the following security practices:

- **Arbitrary shell execution**: `bash_exec` and `bash_curl` run commands and HTTP requests with the caller's permissions. Anything the agent can do, you can do. Sandbox, containerize, or otherwise restrict the agent before granting it these tools, and review which commands it is being asked to run.
- **LLM-driven decisions**: All tools are invoked based on LM output. A prompt-injected or misbehaving model can call any tool it is given, including destructive file writes and network requests. Only expose the tools an agent actually needs.
- **Data exposure**: Tool results (file contents, directory listings, command output, web pages) enter the LM's context and are sent to your LLM provider. Do not point agents at sensitive, personally identifiable, or proprietary data unless you fully trust and have contractual agreements with your provider.
- **Network tools**: `bash_curl`, `bash_fetch`, `web_search`, and `web_news` make outbound requests. An agent with these tools can reach URLs and services you may not intend, and fetched content becomes agent-visible input — a prompt-injection vector. Fetching pages from untrusted sources with file-write tools enabled is an untrusted-input-to-code path.
- **File tool snapshot guard**: `read_file` returns a content hash snapshot, and `edit_file`/`write_file` reject changes if the file changed since it was read. This protects against concurrent edits, not against malicious writes — an agent with `write_file` can still modify any path it is given.
- **DSPy dependency**: Security properties are bounded by DSPy's own guarantees for `ReAct` and `RLM` (including the RLM sandbox for model-written Python). Always pin to a known-compatible DSPy version (requires DSPy 3.1+).

## Security Hall of Fame

We would like to thank the following security researchers for responsibly disclosing security issues to us.

*No security researchers have been added to the hall of fame yet. Will you be the first?*