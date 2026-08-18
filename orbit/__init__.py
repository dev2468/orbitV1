"""Orbit runtime package.

Silences google-adk's [EXPERIMENTAL] feature UserWarnings on import.
They're informational, but PowerShell renders anything a process writes to
stderr as a red NativeCommandError block, which makes a perfectly
successful run look like a crash. Filtered narrowly (only this specific
message pattern) so genuine warnings still surface.
"""

import warnings

warnings.filterwarnings("ignore", message=r".*\[EXPERIMENTAL\].*", category=UserWarning)
