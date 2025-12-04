from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any

import stripe
from django.http import JsonResponse

from payments import PaymentError
from payments import PaymentStatus
from payments import RedirectNeeded
from payments.core import BasicProvider
from payments.forms import PaymentForm as BasePaymentForm


@dataclass
class StripeProductData:
    name: str
    description: str | None = field(init=False, repr=False, default=None)
    images: str | None = field(init=False, repr=False, default=None)
    metadata: dict | None = field(init=False, repr=False, default=None)
    tax_code: str | None = field(init=False, repr=False, default=None)


@dataclass
class StripePriceData:
    currency: str
    product_data: StripeProductData
    unit_amount: int
    recurring: dict | None = field(init=False, repr=False, default=None)
    tax_behavior: str | None = field(init=False, repr=False, default=None)


@dataclass
class StripeLineItem:
    price_data: StripePriceData
    quantity: int
    adjustable_quantity: dict | None = field(init=False, repr=False, default=None)
    dynamic_tax_rates: dict | None = field(init=False, repr=False, default=None)
    tax_rates: str | None = field(init=False, repr=False, default=None)


zero_decimal_currency: list = [
    "bif",
    "clp",
    "djf",
    "gnf",
    "jpy",
    "kmf",
    "krw",
    "mga",
    "pyg",
    "rwf",
    "ugx",
    "vnd",
    "vuv",
    "xaf",
    "xof",
    "xpf",
]
stripe_enabled_events: list = [
    "checkout.session.expired",
    "checkout.session.async_payment_failed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.completed",
]

stripe_payment_intent_events: list = [
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "payment_intent.requires_action",
]


class StripeProviderV3(BasicProvider):
    """Provider backend using `Stripe <https://stripe.com/>`_ api version 3.

    :param api_key: Secret key assigned by Stripe.
    :param use_token: Use instance.token instead of instance.pk in client_reference_id
    :param endpoint_secret: Endpoint Signing Secret.
    :param secure_endpoint: Validate the recieved data, useful for development.
    :param recurring_payments: Enable wallet-based recurring payments (server-initiated).
    :param store_payment_method: Store PaymentMethod for future use.
    """

    form_class = BasePaymentForm

    def __init__(
        self,
        api_key,
        use_token=True,
        endpoint_secret=None,
        secure_endpoint=True,
        recurring_payments=False,
        store_payment_method=False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.api_key = api_key
        self.use_token = use_token
        self.endpoint_secret = endpoint_secret
        self.secure_endpoint = secure_endpoint
        self.recurring_payments = recurring_payments
        self.store_payment_method = store_payment_method or recurring_payments

    def get_form(self, payment, data=None):
        if not payment.transaction_id:
            try:
                session = self.create_session(payment)
            except PaymentError as pe:
                payment.change_status(PaymentStatus.ERROR, str(pe))
                raise pe
            else:
                payment.attrs.session = session
                payment.transaction_id = session.get("id", None)
                payment.save()

        if "url" not in payment.attrs.session:
            raise PaymentError("Stripe returned a session without a URL")

        raise RedirectNeeded(payment.attrs.session.get("url"))

    def create_session(self, payment):
        """Makes the call to Stripe to create the Checkout Session"""
        if not payment.transaction_id:
            stripe.api_key = self.api_key
            session_data = {
                "line_items": self.get_line_items(payment),
                "mode": "payment",
                "success_url": payment.get_success_url(),
                "cancel_url": payment.get_failure_url(),
                "client_reference_id": payment.token if self.use_token else payment.pk,
            }

            # Enable payment method storage for recurring payments
            if self.store_payment_method:
                session_data["payment_intent_data"] = {
                    "setup_future_usage": "off_session",
                }

            # Patch session with billing email if exists
            if payment.billing_email:
                session_data.update({"customer_email": payment.billing_email})

            # Patch session with billing name
            if payment.billing_first_name or payment.billing_last_name:
                session_data.update(
                    {
                        "metadata": {
                            "customer_name": f"{payment.billing_first_name} "
                            f"{payment.billing_last_name}"
                        }
                    }
                )
            try:
                return stripe.checkout.Session.create(**session_data)
            except stripe.StripeError as e:
                # Payment has been declined by Stripe, check Stripe Dashboard
                raise PaymentError(e) from e
        else:
            raise PaymentError("This payment has already been processed.")

    def refund(self, payment, amount=None) -> int:
        if payment.status == PaymentStatus.CONFIRMED:
            to_refund = amount or payment.total
            try:
                payment_intent = payment.attrs.session["payment_intent"]
            except Exception as e:
                raise PaymentError("Can't Refund, payment_intent does not exist") from e

            stripe.api_key = self.api_key
            try:
                refund = stripe.Refund.create(
                    payment_intent=payment_intent,
                    amount=self.convert_amount(payment.currency, to_refund),
                    reason="requested_by_customer",
                )
            except stripe.StripeError as e:
                raise PaymentError(e) from e
            else:
                payment.attrs.refund = json.dumps(refund)
                payment.save()
                payment.change_status(PaymentStatus.REFUNDED)
                return self.convert_amount(payment.currency, to_refund)

        raise PaymentError("Only Confirmed payments can be refunded")

    def status(self, payment):
        if payment.status == PaymentStatus.WAITING:
            stripe.api_key = self.api_key
            session = stripe.checkout.Session.retrieve(payment.transaction_id)
            if session.payment_status == "paid":
                payment.change_status(PaymentStatus.CONFIRMED)
                payment.attrs.session = session
                payment.save()

        return payment

    def get_line_items(self, payment) -> list:
        order_no = payment.token if self.use_token else payment.pk
        product_data = StripeProductData(name=f"Order #{order_no}")

        price_data = StripePriceData(
            currency=payment.currency.lower(),
            unit_amount=self.convert_amount(payment.currency, payment.total),
            product_data=product_data,
        )
        line_item = StripeLineItem(
            quantity=1,
            price_data=price_data,
        )
        # https://stacktuts.com/how-to-ignore-none-values-using-asdict-in-dataclasses
        return [asdict(line_item)]

    def convert_amount(self, currency, amount) -> int:
        # Check if the currency has to be converted to cents
        factor = 100 if currency.lower() not in zero_decimal_currency else 1

        return int(amount * factor)

    def return_event_payload(self, request) -> Any:
        if self.secure_endpoint:
            if "STRIPE_SIGNATURE" not in request.headers:
                raise PaymentError(
                    code=400, message="STRIPE_SIGNATURE not in request.headers"
                )

            try:
                return stripe.Webhook.construct_event(
                    request.body,
                    request.headers["STRIPE_SIGNATURE"],
                    self.endpoint_secret,
                )
            except ValueError as e:
                # Invalid payload
                raise e
            except stripe.SignatureVerificationError as e:
                # Invalid signature
                raise e
        else:
            return json.loads(request.body)

    def get_token_from_request(self, payment, request) -> str:
        """Return payment token from provider request."""
        stripe.api_key = self.api_key
        event = self.return_event_payload(request)

        try:
            return event["data"]["object"]["client_reference_id"]
        except Exception as e:
            raise PaymentError(
                code=400,
                message="client_reference_id is not present, check Stripe Dashboard.",
            ) from e

    def autocomplete_with_wallet(self, payment):
        """
        Complete payment using stored PaymentMethod (server-initiated recurring payment).

        This method charges a stored payment method without user interaction.
        If 3D Secure or other authentication is required, raises RedirectNeeded.
        """
        stripe.api_key = self.api_key

        # Get stored PaymentMethod token
        payment_method_id = payment.get_renew_token()
        if not payment_method_id:
            raise PaymentError("No payment method token found for recurring payment")

        try:
            # Create PaymentIntent with stored PaymentMethod
            intent = stripe.PaymentIntent.create(
                amount=self.convert_amount(payment.currency, payment.total),
                currency=payment.currency.lower(),
                payment_method=payment_method_id,
                confirm=True,  # Immediately attempt to charge
                off_session=True,  # Server-initiated, user not present
                metadata={
                    "payment_token": payment.token,
                    "payment_id": payment.pk if not self.use_token else None,
                },
            )

            payment.transaction_id = intent.id
            payment.attrs.payment_intent = intent
            payment.save()

            # Handle immediate response
            if intent.status == "succeeded":
                payment.captured_amount = payment.total
                payment.change_status(PaymentStatus.CONFIRMED)
                self._finalize_wallet_payment(payment)

            elif intent.status == "requires_action":
                # 3D Secure or other authentication needed
                if intent.next_action and intent.next_action.type == "redirect_to_url":
                    redirect_url = intent.next_action.redirect_to_url.url
                    raise RedirectNeeded(redirect_url)
                else:
                    raise PaymentError(f"Payment requires action: {intent.next_action}")

            elif intent.status in ["requires_payment_method", "canceled"]:
                # Payment failed
                error_message = "Payment failed"
                if intent.last_payment_error:
                    error_message = intent.last_payment_error.message
                payment.change_status(PaymentStatus.REJECTED, error_message)

            else:
                # Other status (processing, requires_capture, etc.)
                payment.change_status(PaymentStatus.WAITING)

        except stripe.error.CardError as e:
            # Card was declined
            payment.change_status(PaymentStatus.REJECTED, str(e))
            raise PaymentError(f"Card declined: {e}") from e

        except stripe.error.StripeError as e:
            # Other Stripe error
            payment.change_status(PaymentStatus.ERROR, str(e))
            raise PaymentError(f"Stripe error: {e}") from e

    def erase_wallet(self, wallet):
        """
        Detach PaymentMethod from customer (if applicable).

        This prevents the payment method from being charged again.
        """
        stripe.api_key = self.api_key

        if wallet.token:
            try:
                payment_method = stripe.PaymentMethod.retrieve(wallet.token)
                # Only detach if attached to a customer
                if hasattr(payment_method, "customer") and payment_method.customer:
                    payment_method.detach()
            except stripe.error.StripeError:
                # Payment method doesn't exist or already detached
                pass

        super().erase_wallet(wallet)

    def process_data(self, payment, request):
        """Processes the event sent by stripe.

        Updates the payment status and adds the event to the attrs property
        """
        event = self.return_event_payload(request)
        event_type = event.get("type")

        # Handle Checkout Session events (one-time payments)
        if event_type in stripe_enabled_events:
            try:
                session_info = event["data"]["object"]
            except Exception as e:
                raise PaymentError(
                    code=400, message="session not present, check Stripe Dashboard"
                ) from e

            if session_info["status"] == "expired":
                # Expired Order
                payment.change_status(PaymentStatus.REJECTED)

            elif session_info["payment_status"] == "paid":
                # Paid Order
                payment.change_status(PaymentStatus.CONFIRMED)

                # Store PaymentMethod for recurring payments
                if self.store_payment_method and hasattr(payment, "set_renew_token"):
                    self._store_payment_method_from_session(payment, session_info)

            payment.attrs.session = session_info
            payment.save()

        # Handle PaymentIntent events (recurring payments)
        elif event_type in stripe_payment_intent_events:
            return self._process_payment_intent_webhook(payment, event)

        return JsonResponse({"status": "OK"})

    def _store_payment_method_from_session(self, payment, session_info):
        """
        Extract and store PaymentMethod from successful Checkout Session.
        """
        stripe.api_key = self.api_key

        try:
            # Get PaymentIntent from session
            payment_intent_id = session_info.get("payment_intent")
            if not payment_intent_id:
                return

            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            payment_method_id = payment_intent.payment_method

            if not payment_method_id:
                return

            # Get PaymentMethod details
            payment_method = stripe.PaymentMethod.retrieve(payment_method_id)

            # Extract card details
            card_data = {}
            if payment_method.type == "card" and payment_method.card:
                card_data = {
                    "card_expire_year": payment_method.card.exp_year,
                    "card_expire_month": payment_method.card.exp_month,
                    "card_masked_number": payment_method.card.last4,
                }

            # Store token
            payment.set_renew_token(
                token=payment_method_id,
                automatic_renewal=True,
                **card_data,
            )

        except stripe.error.StripeError:
            # Failed to retrieve payment method, but payment was successful
            # Don't fail the payment, just skip storing the method
            pass

    def _process_payment_intent_webhook(self, payment, event):
        """
        Handle PaymentIntent webhooks for recurring payments.
        """
        try:
            intent = event["data"]["object"]
        except Exception as e:
            raise PaymentError(
                code=400, message="payment_intent not present in webhook"
            ) from e

        # Verify this is our payment
        if payment.transaction_id != intent.id:
            return JsonResponse({"status": "OK", "message": "Payment ID mismatch"})

        event_type = event.get("type")

        if event_type == "payment_intent.succeeded":
            payment.captured_amount = payment.total
            payment.change_status(PaymentStatus.CONFIRMED)
            payment.attrs.payment_intent = intent
            payment.save()
            self._finalize_wallet_payment(payment)

        elif event_type == "payment_intent.payment_failed":
            error_message = "Payment failed"
            if intent.get("last_payment_error"):
                error_message = intent["last_payment_error"].get("message", error_message)
            payment.change_status(PaymentStatus.REJECTED, error_message)
            payment.attrs.payment_intent = intent
            payment.save()

        elif event_type == "payment_intent.requires_action":
            # Payment requires user action (3DS)
            payment.change_status(PaymentStatus.INPUT)
            payment.attrs.payment_intent = intent
            payment.save()

        return JsonResponse({"status": "OK"})
