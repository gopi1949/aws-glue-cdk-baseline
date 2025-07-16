from aws_cdk import (
    Stack,
    SecretValue,
    Environment,
    aws_iam as iam,
    aws_codebuild as codebuild
)
from aws_cdk.pipelines import CodePipeline, CodePipelineSource, CodeBuildStep, ManualApprovalStep
from constructs import Construct
from typing import Dict
from aws_glue_cdk_baseline.glue_app_stage import GlueAppStage


class PipelineStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, config: Dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # GitHub as source
        source = CodePipelineSource.git_hub(
            "gopi1949/aws-glue-cdk-baseline",  # update if needed
            "main",
            authentication=SecretValue.secrets_manager("github-token")
        )

        # Create a CodeBuild project using external buildspec.yml
        build_project = codebuild.PipelineProject(
            self, "GlueBuildProject",
            build_spec=codebuild.BuildSpec.from_source_filename("buildspec.yml"),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0
            )
        )

        # CDK Pipeline
        pipeline = CodePipeline(self, "GluePipeline",
            pipeline_name="GluePipeline",
            synth=CodeBuildStep("CdkSynth",
                input=source,
                project=build_project,  # ✅ use the external buildspec.yml
                primary_output_directory="cdk.out"
            )
        )

        # Deploy to Dev
        dev_env = Environment(
            account=str(config["devAccount"]["awsAccountId"]),
            region=config["devAccount"]["awsRegion"]
        )
        dev_stage = GlueAppStage(self, "DeployDev", config=config, stage="dev", env=dev_env)
        dev_stage_instance = pipeline.add_stage(dev_stage)

        # Integration test (optional)
        dev_stage_instance.add_post(
            CodeBuildStep("IntegrationTest",
                input=source,
                commands=[
                    f"python $(pwd)/tests/integ/integ_test_glue_app_stack.py --account {config['devAccount']['awsAccountId']} --region {config['devAccount']['awsRegion']} --stage-name DeployDev --sts-role-arn {dev_stage.iam_role_arn}"
                ],
                role_policy_statements=[
                    iam.PolicyStatement(
                        actions=["sts:AssumeRole"],
                        resources=["*"]
                    )
                ]
            )
        )

        # Deploy to Prod (manual approval)
        prod_env = Environment(
            account=str(config["prodAccount"]["awsAccountId"]),
            region=config["prodAccount"]["awsRegion"]
        )
        prod_stage = GlueAppStage(self, "DeployProd", config=config, stage="prod", env=prod_env)
        pipeline.add_stage(prod_stage, pre=[ManualApprovalStep("Approval")])