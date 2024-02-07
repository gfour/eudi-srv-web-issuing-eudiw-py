# coding: latin-1
###############################################################################
# Copyright (c) 2023 European Commission
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
###############################################################################
"""
The PID Issuer Web service is a component of the PID Provider backend. 
Its main goal is to issue the PID and MDL in cbor/mdoc (ISO 18013-5 mdoc) and SD-JWT format.


This boot_validate.py file includes different validation functions.
"""

import base64
from flask import session
import validators

from typing import List
from werkzeug import datastructures
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from tinyec import (registry, ec)
from flask_api import status 
from urllib.parse import urlparse

from crypto_func import decrypt_ECC
from redirect_func import redirect_getpid_or_mdl
from app_config.config_service import ConfService as cfgserv
from app_config.config_countries import ConfCountries as cfgcountries


# Log
from app_config.config_service import ConfService as log


def validate_mandatory_args(args:datastructures.ImmutableMultiDict[str, str], mandlist:List[str]):
    """Validate mandatory query arguments.
    Verify if all the members of mandlist have a value in args

    Keyword arguments:
    + args -- list of query arguments
    + mandlist -- list of strings that need to have a value in args

    Return: Return tuple (bool, List[str]). 
    + If all the mandlist elements have a value in args, return (true, []). 
    + If there are mandlist elements that have not a value in args, return (false, l), where l is the list of all mandlist elements that have no value in args.
    """
    l = []
    b = True

    for m in mandlist:
        if args.get(m) is None:
            b = False
            l.append(m)
    return (b, l)


def validate_cert_algo(certificate, lalgo):
    """Validate if certificate algorithm and curve is in the list (lalgo) of supported algorithms 
    
    Keyword arguments:
    + certificate -- certificate in PEM format
    + lalgo -- list of supported algorithms

    Return: (b, algo, curve), where 
    + b is True if the certificate algorithm is in the list (lalgo) of supported algorithms, and false otherwise;
    + algo is the certificate algoritm name
    + curve is the public key curve name
    """
    try:
        cert = x509.load_pem_x509_certificate(certificate)
    except Exception as e: 
        return(False, str(e), "unknown")
    algname = cert.signature_algorithm_oid._name
    curvname = cert.public_key().curve.name

    if algname not in lalgo: # validate certificate algorithm
        return (False, algname, curvname)
    if curvname not in lalgo[algname]: # validate public key curve
        return (False, algname, curvname)

    return (True, algname, curvname)


def validate_getpidtest_result(ciphertext, nonce, authTag, ciphertextPubKey, plaintext, privatekey):
    """Validate text ciphered (ECC-Based Hybrid Encryption (using ECDH) + AES-256-GCM) by /pid/getpidtest using eccEnc function (in crypto_func.py)
    
    Keyword arguments:
    + ciphertext - ciphered plaintext (AES-256-GCM) with a symetric key derived from the certificate public key
    + nonce - random AES initialization vector
    + authTag - MAC code of the encrypted text, obtained by the GCM block mode
    + ciphertextPubKey - randomly generated ephemeral public key (DER format), that will be used by the ciphertext receiver to derive the symmetric encryption key, using the ECDH key agreement scheme.
    + plaintext - original plaintext
    + privatekey - private key (PEM format) to derive the symmetric encryption key

    Return: True if decrypted ciphertext is equal to plaintex. False, otherwise
    """
    # ciphertextPubKey in Point type
    pub = ec.Point(registry.get_curve(
        ciphertextPubKey.public_numbers().curve.name), 
        ciphertextPubKey.public_numbers().x, 
        ciphertextPubKey.public_numbers().y
        )

    decryptedMsg = decrypt_ECC(ciphertext, nonce, authTag, pub, privatekey.private_numbers().private_value)

    return decryptedMsg.decode() == plaintext


def validate_params_getpid_or_mdl(args, list):
    """Validate GET params from /pid/getpid or /mdl/getmdl route
    
    Keyword arguments:
    + args -- params from /pid/getpid or /mdl/getmdl route
    + l -- list of mandatory params from /pid/getpid or /mdl/getmdl route

    Return: Return True or return value.
    + If the args are valid, return True.
    + If a validation of the args fails, return an HTTP error or a redirect
    """
    (b, l) = validate_mandatory_args(args, list)

    #if no device_publickey
    if 'device_publickey' in l:
        log.logger_info.warn(" - WARN - " + session["route"] + " - " + cfgserv.error_list['15'])
        return "Error 15: " + cfgserv.error_list['15'] + "\n", status.HTTP_400_BAD_REQUEST
    
    session['device_publickey'] = args['device_publickey']

    # if no returnURL
    if 'returnURL' in l:
        log.logger_info.warn(" - WARN - " + session["route"] + " - " + args['device_publickey'] + " - " + cfgserv.error_list['11'])
        return "Error 11: " + cfgserv.error_list['11'] + "\n", status.HTTP_400_BAD_REQUEST
    
    session['returnURL'] = args['returnURL']

    # if not well-formed returnURL
    if not validators.url(args['returnURL']):
        urlval = urlparse(args['returnURL'])
        if not urlval.scheme:
            log.logger_info.warn(" - WARN - " + session["route"] + " - " + args['device_publickey'] + " - " + cfgserv.error_list['14'])
            return "Error 14: " + cfgserv.error_list['14'] + "\n", status.HTTP_400_BAD_REQUEST


    # if country not supported
    if not 'country' in l and args['country'] not in cfgcountries.supported_countries.keys():
        log.logger_info.warn(" - WARN - " + session["route"] + " - " + args['device_publickey'] + " - " + cfgserv.error_list['102'])
        return redirect_getpid_or_mdl(session['version'], args['returnURL'], 102, [])
    # if some mandatory parameters are missing
    if not b:
        log.logger_info.warn(" - WARN - " + session["route"] + " - " + args['device_publickey'] + " - " + cfgserv.error_list['101'])
        return redirect_getpid_or_mdl(session['version'], args['returnURL'], 101, [])
    # if no valid certificate
    try:
        certificate = base64.urlsafe_b64decode(args['certificate'])
    except Exception as e:  # catch *all* exceptions
        log.logger_info.warn(" - WARN - " + session["route"] + " - " + args['device_publickey'] + " - " + cfgserv.error_list['103'])
        return redirect_getpid_or_mdl(session['version'], args['returnURL'], 103, [('error_str', 'Certificate not correctly encoded - '+ str(e))])
    # if certificate curve or algorithms not supported
    (v, algo, curve) = validate_cert_algo(certificate, cfgserv.cert_algo_list)
    if not v:
        log.logger_info.warn(" - WARN - " + session["route"] + " - " + args['device_publickey'] + " - " + cfgserv.error_list['104'])
        return redirect_getpid_or_mdl(session['version'], args['returnURL'], 104, [('error_str', 'Certificate algorithm (' + algo + ') or curve (' + curve + ') not supported.')])
    if not'device_publickey' in l :
        device_pub= base64.urlsafe_b64decode(args['device_publickey'].encode('utf-8'))
        if is_valid_pem_public_key(device_pub) == False:
            log.logger_info.warn(" - WARN - " + session["route"] + " - " + cfgserv.error_list['16'])
            return "Error 16: " + cfgserv.error_list['16'] + "\n", status.HTTP_400_BAD_REQUEST
    # if args are valid
    return True



def validate_params_showpid_or_mdl(args, list):
    """Validate GET params from /pid/show or /mdl/getmdl route
    
    Keyword arguments:
    + args -- params from /pid/show or /mdl/getmdl route
    + l -- list of mandatory params from /pid/show or /mdl/getmdl route

    Return: Return True or return value.
    + If the args are valid, return True.
    + If a validation of the args fails, return an HTTP error
    """
    (b, l) = validate_mandatory_args(args, list)
    # if missing mandatory fields or no error field
    if not b or 'error' in l:
        log.logger_info.warn(" - WARN - " + session["route"] + " - " + session['device_publickey'] + " - " + cfgserv.error_list['101'])
        return "Error 101: " + cfgserv.error_list['101'], status.HTTP_206_PARTIAL_CONTENT
    # if no error field
    if int(args['error']) != 0:
        err = str(args['error'])
        return "Error " + err + ": " + args['error_str'], status.HTTP_203_NON_AUTHORITATIVE_INFORMATION
    
    # if args are valid
    return True

def is_valid_pem_public_key(pem_key):
    """ Validate if device public key is in PEM format
    Keyword arguments:
    + pem_key- device public key from /mdl or /pid

    Return: Return True or return value.
    + If pem_key have the correct format , return True.
    + If pem_key have the incorrect format, return False
    """
    try:
        # Attempts to load the public key in PEM format
        public_key = serialization.load_pem_public_key(pem_key, backend=None)
        return True
    except Exception as e:
        return False


