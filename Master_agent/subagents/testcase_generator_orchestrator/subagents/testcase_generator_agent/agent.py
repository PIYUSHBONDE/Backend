"""
Testcase Generator Root Agent

This module defines the root agent for the Testcase generation application.
It uses a sequential agent with an initial testcase generator followed by a refinement loop.
"""

from google.adk.agents import LoopAgent, SequentialAgent

from .subagents.testcase_generator import initial_testcase_generator
from .subagents.testcase_refiner import testcase_refiner
from .subagents.testcase_reviewer import testcase_reviewer

# Create the Refinement Loop Agent
refinement_loop = LoopAgent(
    name="TestcaseRefinementLoop",
    max_iterations=1,
    sub_agents=[
        testcase_reviewer,
        testcase_refiner,
    ],
    description="Iteratively reviews and refines a Testcase until quality requirements are met",
)

# Create the Sequential Pipeline
testcase_generator_agent = SequentialAgent(
    name="TestcaseGenerationEnginePipeline",
    sub_agents=[
        initial_testcase_generator,  # Step 1: Generate initial Testcase
        refinement_loop,  # Step 2: Review and refine in a loop
    ],
    description="Generates and refines a Testcase through an iterative review process",
)
