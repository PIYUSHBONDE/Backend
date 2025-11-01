"""
Feature Manager Agent

This agent removes first element from features_to_process list.
"""

from google.adk.agents.llm_agent import LlmAgent


# Constants
GEMINI_MODEL = "gemini-2.0-flash"

# Define the Testcase Collector Agent
feature_manager = LlmAgent(
    name="FeatureManager",
    model=GEMINI_MODEL,
    instruction="""
### **Instructions for Feature Manager Agent**
### Input
{features_to_process}
Your sole responsibility is to access a list named **`features_to_extract`** from the session state, remove its first element, and save the modified list back to the session state.

1.  **Access the list**: Retrieve the list `features_to_extract` from the session state.
2.  **Verify the list is not empty**: Check if the list contains any items.
3.  **Remove the first element**: If the list is not empty, create a new version of the list that excludes the first element.
4.  **Update the session state**: Save this new, shortened list back into the session state, replacing the original `features_to_extract` list.
5.  **Handle empty list**: If the list is empty or does not exist, do nothing.
    """,
    description="Removes the first element from features_to_process list",
    output_key="features_to_process",
)
