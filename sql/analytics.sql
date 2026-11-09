-- This view uses the customer profile that was valid when the transaction happened.
-- valid_to is exclusive, so an event at a change boundary matches exactly one version.
CREATE OR REPLACE VIEW daily_risk_summary AS
SELECT
    CAST(f.transaction_time AS DATE) AS transaction_date,
    d.risk_rating,
    d.kyc_status,
    f.channel,
    f.currency,
    COUNT(*) AS transaction_count,
    SUM(f.amount) AS total_amount
FROM fact_transactions f
JOIN dim_customer d
    ON f.customer_id = d.customer_id
    AND f.transaction_time >= d.valid_from
    AND (f.transaction_time < d.valid_to OR d.valid_to IS NULL)
WHERE f.status = 'SUCCESS'
GROUP BY 1, 2, 3, 4, 5;

-- All supplied amounts use INR. Failed transactions remain in the fact table but
-- are excluded from this successful-transaction report.
SELECT * FROM daily_risk_summary
ORDER BY transaction_date, risk_rating, kyc_status, channel;
