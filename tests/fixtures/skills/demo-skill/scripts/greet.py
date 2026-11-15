import os
import sys

print("greet:", " ".join(sys.argv[1:]))
print("secret:", os.environ.get("SKILL_TEST_SECRET", "<unset>"))
print("cwd:", os.getcwd())
