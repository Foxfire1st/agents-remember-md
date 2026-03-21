Write a descriptive commit message based on the following instructions:

1) Ask from the user the ticket id starting with a hash (#).
2) Then ask the user about the prior state and why the change was needed
3) Ask the user about the new state and how it improves the code or functionality
4) Then generate a title based on the provided information
5) Choose one appropriate category that describes the changes best.
6) Write the commit message in the specified format including the ticket ID and categories.

Then write the commit message in the following format:
Type: #TicketID [CATEGORIES]: [Title of the change]
Prior State: [Description of the prior state and why the change was needed]
New State: [Description of the new state and how it improves the code or functionality]

CATEGORIES of commit messages:
BUILD
CHORE
CI
DOCS
FEATURE
FIX
PERFORMANCE
REFACTOR
REVERT
