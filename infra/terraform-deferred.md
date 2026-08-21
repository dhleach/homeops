# Deferred Terraform changes

Status: active drift guard, verified 2026-08-21.

## Why this exists

The plan captured from the Pi before this guard was added reported:

```text
Plan: 16 to add, 0 to change, 4 to destroy.
```

The intended Ask HomeOps resources were mixed with unrelated replacement work.
The four destructive actions were:

- `aws_instance.homeops`: the `most_recent` Ubuntu AMI changed and the current
  bootstrap `user_data` hash differs from state.
- `aws_eip_association.homeops`: follows the EC2 replacement.
- `aws_iam_policy.ssm_k3s_token_read` and its attachment: the existing named
  policy's description and document were changed, which forces replacement.

The plan also contained an older, unapplied
`aws_iam_user_policy_attachment.ssm_k3s_token_write` addition. That permission
is unrelated to restoring Ask HomeOps and is no longer in the active graph.

## What this rollout changes

- `aws_iam_policy.ask_homeops_runtime_read` and its role attachment grant the
  EC2 role only `ssm:GetParameter` for `/homeops/<environment>/ask-homeops-*`.
  This is additive and leaves the existing bootstrap policy untouched.
- `aws_instance.homeops.lifecycle.ignore_changes` temporarily ignores only
  `ami` and `user_data`, preventing an accidental replacement while Cognito,
  SSM, and runtime IAM resources are provisioned.
- The old deploy-user SSM-write attachment is removed from the active
  Terraform graph. The underlying policy remains documented state until that
  permission is deliberately reviewed and reintroduced.

## Safe rollout acceptance check

The reviewed plan for Ask HomeOps may add the four Cognito resources, seven
`ask-homeops-*` SSM parameters, and the additive runtime IAM policy/attachment.
It must show:

- no action for `aws_instance.homeops`;
- no action for `aws_eip_association.homeops`;
- no replacement of `aws_iam_policy.ssm_k3s_token_read` or its attachment; and
- no creation of `aws_iam_user_policy_attachment.ssm_k3s_token_write`.

If any of those appear, stop and do not apply the plan.

## How to re-enable the deferred work later

During a planned EC2 maintenance window:

1. Confirm the intended Ubuntu AMI, Terraform state, EIP association, current
   instance data, and bootstrap/user-data changes together.
2. Remove the `ami`/`user_data` lifecycle guard and generate a full plan.
3. Decide whether the deploy-user SSM-write attachment is still required;
   restore it only after reviewing its scope and the live IAM state.
4. Apply the EC2/EIP/bootstrap changes only with an explicit replacement plan,
   a rollback/recovery window, and a post-boot release-gate check.

Do not revive the old `/tmp/homeops-222.tfplan`; it predates the current state
and is not a safe artifact for this work.
