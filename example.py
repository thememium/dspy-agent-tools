from dspy_agent_tools import edit_file, read_file

changelog_path = (
    "/Users/edwardboswell/ghq/github.com/thememium/dspy-agent-tools/CHANGELOG.md"
)

result = read_file(changelog_path)
print(result[1])

content = """### Contributor
Magic test
"""
result = edit_file(
    changelog_path, result[1], start_line=9, end_line=10, new_text=content
)
print(result)


result = read_file(changelog_path)
print(result[0])
