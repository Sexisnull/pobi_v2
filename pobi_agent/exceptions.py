
# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.

"""Custom exception classes for the security research framework.

This module defines custom exception classes for handling various error
conditions in the security research framework, including model provider
errors, agent timeouts, and vulnerability testing failures.
"""

class PobiError(Exception):
    """Base exception pobi"""

class ModelProviderError(PobiError):
    """Model provider specific errors"""

class AgentTimeoutError(PobiError):
    """Analysis exceeded timeout"""

class VulnerabilityTestingError(PobiError):
    """Error during vulnerability testing"""

class AskForApprovalException(Exception):
    """Ask for approval"""