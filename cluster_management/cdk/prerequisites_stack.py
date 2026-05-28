import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class BeiwePrerequisitesStack(Stack):
    """
    Provisions the manual AWS console steps from the Beiwe deployment wiki:

      1. IAM user 'beiwe-deploy' with AdministratorAccess.
         Access key credentials are stored in Secrets Manager as
         'beiwe-deploy-credentials' ({AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY}).

      2. EC2 key pair 'beiwe-deployment-key'.
         CloudFormation stores the private key in SSM Parameter Store at
         /ec2/keypair/{key-pair-id} as a SecureString.

      3. A dedicated VPC for Beiwe (2 public subnets across 2 AZs, no NAT
         gateway). Using a dedicated VPC rather than the account default keeps
         Beiwe's resources isolated from anything else in the account.

    Outputs consumed by cluster_management/setup_beiwe.py:
      KeyPairName           - key pair name for global_configuration.json
      KeyPairId             - key pair ID used to build the SSM parameter path
      CredentialsSecretArn  - Secrets Manager ARN for IAM credentials
      VpcId                 - VPC ID for global_configuration.json
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # IAM user used by launch_script.py and the EB CLI
        user = iam.User(self, "BeiweDeployUser", user_name="beiwe-deploy")
        user.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess")
        )

        # Programmatic access key for the IAM user
        access_key = iam.CfnAccessKey(
            self, "BeiweDeployAccessKey", user_name=user.user_name
        )

        # Store credentials in Secrets Manager so the setup script can retrieve
        # them without ever printing them to stdout.
        # Fn.sub resolves the CloudFormation token references at deploy time.
        credentials_secret = secretsmanager.CfnSecret(
            self,
            "BeiweDeployCredentials",
            name="beiwe-deploy-credentials",
            secret_string=cdk.Fn.sub(
                '{"AWS_ACCESS_KEY_ID":"${AKI}","AWS_SECRET_ACCESS_KEY":"${SAK}"}',
                {"AKI": access_key.ref, "SAK": access_key.attr_secret_access_key},
            ),
        )

        # EC2 key pair for SSH access to EB/processing servers.
        # CloudFormation automatically stores the private key in SSM Parameter
        # Store at /ec2/keypair/{KeyPairId} as a SecureString.
        key_pair = ec2.CfnKeyPair(
            self,
            "BeiweDeployKey",
            key_name="beiwe-deployment-key",
            key_type="rsa",
        )

        # Dedicated VPC for Beiwe.
        # Public subnets only — launch_script.py SSHes to processing servers via their
        # public IP (see elastic_compute_cloud.get_manager_public_ip), so those servers
        # must be publicly reachable. Moving them to private subnets would require
        # replacing the direct-SSH approach with a bastion or SSM Session Manager.
        # RDS is created with PubliclyAccessible=False and is access-controlled by
        # security groups regardless of subnet placement.
        # No NAT gateway needed (and saves ~$35/month) because all instances are public.
        vpc = ec2.Vpc(
            self,
            "BeiweVpc",
            max_azs=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
            nat_gateways=0,
        )

        CfnOutput(self, "KeyPairName", value=key_pair.ref)
        CfnOutput(self, "KeyPairId", value=key_pair.attr_key_pair_id)
        CfnOutput(self, "CredentialsSecretArn", value=credentials_secret.ref)
        CfnOutput(self, "DeployUserArn", value=user.user_arn)
        CfnOutput(self, "VpcId", value=vpc.vpc_id)
