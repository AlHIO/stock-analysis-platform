# Synthetic provider fixtures

These small, fixed records are synthetic test data in the official observed field
shapes. They are never imported as production market data. TWSE may return English
JSON or Chinese-header CSV; TPEx quote/catalog use different date semantics. Live
network checks use the actual official endpoints and have a separate pytest marker.
