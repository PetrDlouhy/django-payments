"""
Tests for wallet-based recurring payments interface.

These tests document the expected behavior of the wallet interface
for server-initiated recurring payments.
"""
from decimal import Decimal
from unittest.mock import Mock

from django.test import TestCase

from payments import PaymentError
from payments import PaymentStatus
from payments import WalletStatus
from payments.core import provider_factory
from testapp.testmain.models import Payment
from testapp.testmain.models import Wallet


class BaseWalletInterfaceTests(TestCase):
    """
    Tests for BaseWallet model interface.

    These tests document the expected behavior of wallet lifecycle:
    1. Create wallet (status=PENDING)
    2. First payment succeeds → wallet.payment_completed() → status=ACTIVE
    3. Cancel subscription → wallet.erase() → status=ERASED
    """

    def test_wallet_starts_pending(self):
        """New wallets start in PENDING status."""
        wallet = Wallet.objects.create(payment_provider="test")
        self.assertEqual(wallet.status, WalletStatus.PENDING)
        self.assertEqual(wallet.token, "")

    def test_payment_completed_activates_wallet(self):
        """payment_completed() activates wallet on first successful payment."""
        wallet = Wallet.objects.create(payment_provider="test")
        payment = Payment.objects.create(
            variant="test",
            status=PaymentStatus.CONFIRMED,
            total=Decimal("20.00"),
            currency="USD",
        )

        wallet.payment_completed(payment)

        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ACTIVE)

    def test_payment_completed_ignores_if_already_active(self):
        """payment_completed() doesn't change status if already active."""
        wallet = Wallet.objects.create(
            payment_provider="test", status=WalletStatus.ACTIVE
        )
        payment = Payment.objects.create(
            variant="test",
            status=PaymentStatus.CONFIRMED,
            total=Decimal("20.00"),
            currency="USD",
        )

        wallet.payment_completed(payment)

        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ACTIVE)

    def test_payment_completed_ignores_non_confirmed_payments(self):
        """payment_completed() doesn't activate on non-confirmed payments."""
        wallet = Wallet.objects.create(payment_provider="test")
        payment = Payment.objects.create(
            variant="test",
            status=PaymentStatus.REJECTED,
            total=Decimal("20.00"),
            currency="USD",
        )

        wallet.payment_completed(payment)

        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.PENDING)

    def test_activate_helper(self):
        """activate() marks wallet as active."""
        wallet = Wallet.objects.create(payment_provider="test")

        wallet.activate()

        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ACTIVE)

    def test_erase_helper(self):
        """erase() marks wallet as erased."""
        wallet = Wallet.objects.create(
            payment_provider="test", status=WalletStatus.ACTIVE, token="test_token"
        )

        wallet.erase()

        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ERASED)
        self.assertEqual(wallet.token, "test_token")  # Token remains for audit

    def test_wallet_extra_data_stores_card_details(self):
        """extra_data can store provider-specific metadata."""
        wallet = Wallet.objects.create(payment_provider="test")
        wallet.extra_data = {
            "card_expire_year": 2025,
            "card_expire_month": 12,
            "card_masked_number": "1234",
        }
        wallet.save()

        wallet.refresh_from_db()
        self.assertEqual(wallet.extra_data["card_expire_year"], 2025)
        self.assertEqual(wallet.extra_data["card_masked_number"], "1234")


class BasePaymentTokenInterfaceTests(TestCase):
    """
    Tests for BasePayment token management interface.

    These tests document the expected behavior of get_renew_token() and
    set_renew_token() methods that enable wallet-based recurring payments.
    """

    def test_get_renew_token_with_wallet(self):
        """get_renew_token() retrieves token from linked wallet."""
        wallet = Wallet.objects.create(
            payment_provider="test",
            token="pm_test_token",
            status=WalletStatus.ACTIVE,
        )
        payment = Payment.objects.create(
            variant="test",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        token = payment.get_renew_token()

        self.assertEqual(token, "pm_test_token")

    def test_get_renew_token_without_wallet(self):
        """get_renew_token() returns None when no wallet."""
        payment = Payment.objects.create(
            variant="test", total=Decimal("20.00"), currency="USD"
        )

        token = payment.get_renew_token()

        self.assertIsNone(token)

    def test_get_renew_token_with_pending_wallet(self):
        """get_renew_token() returns None if wallet not active."""
        wallet = Wallet.objects.create(
            payment_provider="test",
            token="pm_test_token",
            status=WalletStatus.PENDING,
        )
        payment = Payment.objects.create(
            variant="test",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        token = payment.get_renew_token()

        self.assertIsNone(token)  # Not active yet

    def test_set_renew_token_creates_wallet_if_needed(self):
        """set_renew_token() creates wallet if it doesn't exist."""
        payment = Payment.objects.create(
            variant="test", total=Decimal("20.00"), currency="USD"
        )

        payment.set_renew_token(
            token="pm_new_token",
            card_expire_year=2025,
            card_expire_month=12,
            card_masked_number="1234",
        )

        payment.refresh_from_db()
        self.assertIsNotNone(payment.wallet)
        self.assertEqual(payment.wallet.token, "pm_new_token")
        self.assertEqual(payment.wallet.status, WalletStatus.ACTIVE)
        self.assertEqual(payment.wallet.extra_data["card_expire_year"], 2025)

    def test_set_renew_token_updates_existing_wallet(self):
        """set_renew_token() updates existing wallet token."""
        wallet = Wallet.objects.create(
            payment_provider="test", token="old_token", status=WalletStatus.ACTIVE
        )
        payment = Payment.objects.create(
            variant="test",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        payment.set_renew_token(token="new_token", card_masked_number="5678")

        wallet.refresh_from_db()
        self.assertEqual(wallet.token, "new_token")
        self.assertEqual(wallet.extra_data["card_masked_number"], "5678")


class DummyProviderWalletTests(TestCase):
    """
    Tests for DummyProvider wallet implementation.

    DummyProvider serves as reference implementation for wallet-based providers.
    These tests document the expected behavior of autocomplete_with_wallet().
    """

    def test_autocomplete_with_wallet_charges_stored_payment_method(self):
        """autocomplete_with_wallet() charges using stored token."""
        wallet = Wallet.objects.create(
            payment_provider="dummy",
            token="test_payment_method",
            status=WalletStatus.ACTIVE,
        )
        payment = Payment.objects.create(
            variant="dummy",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        provider = provider_factory("dummy")
        provider.autocomplete_with_wallet(payment)

        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(payment.captured_amount, Decimal("20.00"))
        self.assertIn("dummy-wallet-charge", payment.transaction_id)

    def test_autocomplete_with_wallet_fails_without_token(self):
        """autocomplete_with_wallet() raises error if no token found."""
        payment = Payment.objects.create(
            variant="dummy", total=Decimal("20.00"), currency="USD"
        )

        provider = provider_factory("dummy")

        with self.assertRaises(PaymentError) as cm:
            provider.autocomplete_with_wallet(payment)

        self.assertIn("No payment method token", str(cm.exception))

    def test_autocomplete_with_wallet_triggers_payment_completed(self):
        """autocomplete_with_wallet() triggers wallet.payment_completed()."""
        wallet = Wallet.objects.create(
            payment_provider="dummy",
            token="test_payment_method",
            status=WalletStatus.PENDING,  # Not active yet
        )
        payment = Payment.objects.create(
            variant="dummy",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        provider = provider_factory("dummy")
        provider.autocomplete_with_wallet(payment)

        wallet.refresh_from_db()
        # Wallet should be activated via payment_completed hook
        self.assertEqual(wallet.status, WalletStatus.ACTIVE)


class ProviderHelperMethodsTests(TestCase):
    """
    Tests for provider helper methods.

    These tests document the expected behavior of helper methods that
    providers use to interact with the wallet system.
    """

    def test_finalize_wallet_payment_calls_payment_completed(self):
        """_finalize_wallet_payment() triggers wallet.payment_completed()."""
        wallet = Wallet.objects.create(
            payment_provider="test", status=WalletStatus.PENDING
        )
        payment = Payment.objects.create(
            variant="test",
            status=PaymentStatus.CONFIRMED,
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        provider = provider_factory("dummy")
        provider._finalize_wallet_payment(payment)

        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ACTIVE)

    def test_finalize_wallet_payment_with_explicit_wallet(self):
        """_finalize_wallet_payment() accepts explicit wallet parameter."""
        wallet = Wallet.objects.create(
            payment_provider="test", status=WalletStatus.PENDING
        )
        payment = Payment.objects.create(
            variant="test",
            status=PaymentStatus.CONFIRMED,
            total=Decimal("20.00"),
            currency="USD",
        )

        provider = provider_factory("dummy")
        provider._finalize_wallet_payment(payment, wallet=wallet)

        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ACTIVE)

    def test_finalize_wallet_payment_handles_no_wallet(self):
        """_finalize_wallet_payment() doesn't crash if no wallet."""
        payment = Payment.objects.create(
            variant="test",
            status=PaymentStatus.CONFIRMED,
            total=Decimal("20.00"),
            currency="USD",
        )

        provider = provider_factory("dummy")
        provider._finalize_wallet_payment(payment)  # Should not crash

    def test_erase_wallet_marks_as_erased(self):
        """erase_wallet() marks wallet as ERASED."""
        wallet = Wallet.objects.create(
            payment_provider="test", token="test_token", status=WalletStatus.ACTIVE
        )

        provider = provider_factory("dummy")
        provider.erase_wallet(wallet)

        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ERASED)


class WalletWorkflowIntegrationTests(TestCase):
    """
    Integration tests for complete wallet workflow.

    These tests document the end-to-end flow of wallet-based recurring payments:
    1. First payment → store token → activate wallet
    2. Recurring payment → retrieve token → charge
    3. Cancel → erase wallet
    """

    def test_first_payment_flow(self):
        """
        First payment stores token and activates wallet.

        This documents the expected flow when a user subscribes:
        1. Payment created with wallet
        2. Payment processed successfully
        3. Provider stores PaymentMethod via set_renew_token()
        4. Wallet becomes ACTIVE
        """
        # Step 1: Create payment with new wallet
        wallet = Wallet.objects.create(payment_provider="dummy")
        payment = Payment.objects.create(
            variant="dummy",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        # Step 2: Simulate first payment success
        payment.set_renew_token(
            token="pm_first_payment_token",
            card_expire_year=2025,
            card_expire_month=12,
            card_masked_number="4242",
        )

        # Step 3: Verify wallet activated
        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ACTIVE)
        self.assertEqual(wallet.token, "pm_first_payment_token")
        self.assertEqual(wallet.extra_data["card_masked_number"], "4242")

    def test_recurring_payment_flow(self):
        """
        Recurring payment uses stored token.

        This documents the expected flow for automatic renewals:
        1. Wallet exists with token (ACTIVE)
        2. Create new payment linked to wallet
        3. Call autocomplete_with_wallet()
        4. Provider charges stored payment method
        5. Payment succeeds
        """
        # Step 1: Setup - active wallet with token
        wallet = Wallet.objects.create(
            payment_provider="dummy",
            token="pm_stored_token",
            status=WalletStatus.ACTIVE,
        )

        # Step 2: Create recurring payment
        payment = Payment.objects.create(
            variant="dummy",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        # Step 3: Charge stored payment method
        payment.autocomplete_with_wallet()

        # Step 4: Verify payment succeeded
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(payment.captured_amount, Decimal("20.00"))
        self.assertIsNotNone(payment.transaction_id)

    def test_cancel_subscription_flow(self):
        """
        Cancellation erases wallet.

        This documents the expected flow when user cancels subscription:
        1. Wallet is ACTIVE with stored token
        2. User cancels subscription
        3. Provider erases wallet
        4. Wallet status becomes ERASED
        5. Token remains (for audit trail)
        """
        # Step 1: Active wallet
        wallet = Wallet.objects.create(
            payment_provider="dummy",
            token="pm_to_be_erased",
            status=WalletStatus.ACTIVE,
        )

        # Step 2: Cancel subscription
        provider = provider_factory("dummy")
        provider.erase_wallet(wallet)

        # Step 3: Verify wallet erased
        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ERASED)
        self.assertEqual(wallet.token, "pm_to_be_erased")  # Kept for audit

    def test_complete_subscription_lifecycle(self):
        """
        Complete lifecycle: subscribe → renew → cancel.

        This documents the full workflow from subscription to cancellation.
        """
        # Phase 1: User subscribes (first payment)
        wallet = Wallet.objects.create(payment_provider="dummy")
        first_payment = Payment.objects.create(
            variant="dummy",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        # Simulate successful first payment
        first_payment.set_renew_token(token="pm_subscription_token")
        first_payment.change_status(PaymentStatus.CONFIRMED)

        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ACTIVE)

        # Phase 2: Automatic renewal (30 days later)
        renewal_payment = Payment.objects.create(
            variant="dummy",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        renewal_payment.autocomplete_with_wallet()

        renewal_payment.refresh_from_db()
        self.assertEqual(renewal_payment.status, PaymentStatus.CONFIRMED)

        # Phase 3: User cancels
        provider = provider_factory("dummy")
        provider.erase_wallet(wallet)

        wallet.refresh_from_db()
        self.assertEqual(wallet.status, WalletStatus.ERASED)


class WalletInterfaceDocumentationTests(TestCase):
    """
    Documentation tests for wallet interface patterns.

    These tests serve as executable documentation showing different
    implementation patterns for wallet-based recurring payments.
    """

    def test_simple_wallet_pattern_with_fk(self):
        """
        Simple pattern: Payment has direct FK to Wallet.

        This is the recommended pattern for new projects:
        - Payment.wallet = ForeignKey(Wallet)
        - get_renew_token() returns wallet.token
        - set_renew_token() stores in wallet
        """
        # Pattern used in testapp/testmain/models.py
        wallet = Wallet.objects.create(payment_provider="dummy")
        payment = Payment.objects.create(
            variant="dummy",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        # Store token
        payment.set_renew_token(token="pm_example")

        # Retrieve token
        retrieved_token = payment.get_renew_token()

        self.assertEqual(retrieved_token, "pm_example")

    def test_complex_pattern_without_fk(self):
        """
        Complex pattern: Token stored in separate model (e.g., RecurringUserPlan).

        For projects with existing architecture:
        - Override get_renew_token() to get from your model
        - Override set_renew_token() to store in your model
        - No wallet FK needed

        Example (BlenderKit pattern):
            def get_renew_token(self):
                return self.order.user.userplan.recurring.token

            def set_renew_token(self, token, **kwargs):
                self.order.user.userplan.recurring.token = token
                self.order.user.userplan.recurring.save()
        """
        # Simulate by overriding methods
        payment = Payment.objects.create(
            variant="dummy", total=Decimal("20.00"), currency="USD"
        )

        # Mock the pattern (in real code, you'd override the methods)
        original_get = payment.get_renew_token
        original_set = payment.set_renew_token

        stored_token = None

        def mock_get():
            return stored_token

        def mock_set(token, **kwargs):
            nonlocal stored_token
            stored_token = token

        payment.get_renew_token = mock_get
        payment.set_renew_token = mock_set

        # Use the interface
        payment.set_renew_token("custom_storage_token")
        retrieved = payment.get_renew_token()

        self.assertEqual(retrieved, "custom_storage_token")

    def test_provider_doesnt_care_about_storage(self):
        """
        Providers use interface, don't care about storage mechanism.

        This documents that providers only call get_renew_token() and
        set_renew_token() - they don't need to know about wallet FK,
        RecurringUserPlan, or any storage implementation.
        """
        # Setup: Create payment with mock token storage
        payment = Payment.objects.create(
            variant="dummy", total=Decimal("20.00"), currency="USD"
        )

        # Mock storage (could be wallet, RecurringUserPlan, or anything)
        mock_storage = Mock()
        mock_storage.token = "stored_token"

        payment.get_renew_token = Mock(return_value="stored_token")
        payment.set_renew_token = Mock()

        # Provider uses interface without knowing storage details
        provider = provider_factory("dummy")

        # Simulate what provider does internally
        token = payment.get_renew_token()  # Provider gets token
        self.assertEqual(token, "stored_token")

        # Provider stores token after first payment
        payment.set_renew_token("new_token")
        payment.set_renew_token.assert_called_once_with("new_token")


class WalletErrorHandlingTests(TestCase):
    """
    Tests for error conditions in wallet-based payments.

    These tests document how the wallet interface handles various error scenarios.
    """

    def test_autocomplete_with_wallet_requires_token(self):
        """Attempting to charge without token raises clear error."""
        payment = Payment.objects.create(
            variant="dummy", total=Decimal("20.00"), currency="USD"
        )

        with self.assertRaises(PaymentError) as cm:
            payment.autocomplete_with_wallet()

        self.assertIn("No payment method token", str(cm.exception))

    def test_wallet_can_store_provider_error_details(self):
        """Wallet extra_data can store error details for debugging."""
        wallet = Wallet.objects.create(payment_provider="test")

        wallet.extra_data["last_error"] = {
            "code": "card_declined",
            "message": "Insufficient funds",
            "timestamp": "2025-12-04T10:00:00Z",
        }
        wallet.save()

        wallet.refresh_from_db()
        self.assertEqual(wallet.extra_data["last_error"]["code"], "card_declined")

    def test_erased_wallet_cannot_be_used(self):
        """Erased wallets return None from get_renew_token()."""
        wallet = Wallet.objects.create(
            payment_provider="dummy",
            token="pm_erased_token",
            status=WalletStatus.ERASED,
        )
        payment = Payment.objects.create(
            variant="dummy",
            total=Decimal("20.00"),
            currency="USD",
            wallet=wallet,
        )

        token = payment.get_renew_token()

        self.assertIsNone(token)  # Won't return token from erased wallet

