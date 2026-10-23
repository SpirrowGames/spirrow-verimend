# Deploys

`deploy_request` only records a request; it does not deploy.
`deploy_approve` takes the `request_id`
and the approver in `approved_by`.
`deploy_status` reads a request by `request_id`.
