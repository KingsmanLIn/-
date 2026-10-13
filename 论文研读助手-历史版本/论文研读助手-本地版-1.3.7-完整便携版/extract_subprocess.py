"""Compatibility facade for the bounded PDF worker."""
from pdf_worker import (extract_in_subprocess, scrub_environment, CHILD_SCRIPT, CHILD_FLAG,
                        EXTRACT_TIMEOUT_SECONDS, MAX_CHILD_OUTPUT_BYTES)
