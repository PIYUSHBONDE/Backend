
from google.adk.agents import LoopAgent, SequentialAgent

from .subagents.enhancer_engine import enhancer_engine
from .subagents.feature_manager.TestCaseProcessorAgent import TestCaseProcessorAgent


enhancer_engine_agent = SequentialAgent(
    name="enhancerEnginePipeline",
    sub_agents=[
        enhancer_engine,  # Step 1: Enhance Testcase based on user input
        TestCaseProcessorAgent(),  # Step 2: Collect and format testcases
    ],
    description="Handles users enhancement requests for testcases based on prior conversation context.",
)